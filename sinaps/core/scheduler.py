# -*- coding: utf-8 -*-
"""Расписание, переживающее перезапуск ассистента.

Почему это в ядре, а не в плагинах. Таймер, который живёт в процессе
плагина, умирает вместе с ассистентом: человек ставит напоминание,
выключает компьютер, возвращается — и звонка нет. Если каждый плагин
решает это сам, у каждого будет своё хранилище, своя обработка
«пропущено, пока меня не было» и своя ошибка в этом. Здесь решено один
раз, и плагину остаётся сказать «через десять минут» и получить вызов.

Устройство:

* сроки лежат в SQLite рядом с историей — отдельная таблица, не
  мешает диалогам и переживает перезапуск;
* выбранное место хранения — `data/plans.db`: таймеры не должны жить
  в базе истории, иначе очистка истории стирала бы напоминания, а
  человек удивлялся бы, почему вместе с диалогами пропали планы;
* ядро держит по задаче asyncio на каждый срок и будит плагин;
* при старте просроченное не выбрасывается, а срабатывает сразу: если
  человек просил напомнить в три часа, а вернулся в пять — он всё
  ещё должен это услышать, иначе обещание молча нарушено.

Что здесь нет намеренно: повторяющихся задач («каждый день в девять»).
Место для них есть — `recurring`, — но обещать их, пока они не
сделаны, нельзя.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id         TEXT PRIMARY KEY,
    plugin     TEXT NOT NULL,
    due_at     REAL NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    fired_at   REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS plans_pending ON plans (fired_at, due_at);
"""

#: Плагин, которому нужно позвать. Ключ — имя плагина, значение —
#: функция, ждущая `payload`.
Fires = dict[str, Callable[[dict], Awaitable[None]]]


@dataclass
class Plan:
    """Отложенное дело одного плагина."""

    id: str
    plugin: str
    due_at: float
    payload: dict = field(default_factory=dict)
    created_at: float = 0.0
    fired_at: float | None = None

    @property
    def pending(self) -> bool:
        return self.fired_at is None

    def due_in(self) -> float:
        return max(0.0, self.due_at - time.time())


class PlanStore:
    """Хранилище сроков. Одна таблица, один файл.

    Отдельный файл, а не таблица в базе истории: очистка истории не должна
    стирать напоминания, и наоборот.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def add(self, plan_id: str, plugin: str, due_at: float,
            payload: dict | None = None) -> Plan:
        """Записать срок. Повтор с тем же `plan_id` переписывает.

        Переписывание, а не отказ: человек, сказавший «отмени» и тут же
        «поставь», не должен получать отказ из-за того, что запись ещё
        висит.
        """
        now = time.time()
        body = json.dumps(payload or {}, ensure_ascii=False)
        with self._connect() as con:
            con.execute(
                "INSERT INTO plans (id, plugin, due_at, payload, fired_at,"
                " created_at) VALUES (?, ?, ?, ?, NULL, ?)"
                " ON CONFLICT(id) DO UPDATE SET due_at=excluded.due_at,"
                " payload=excluded.payload, fired_at=NULL",
                (plan_id, plugin, due_at, body, now))
        return Plan(plan_id, plugin, due_at, payload or {}, now)

    def pending(self, plugin: str | None = None) -> list[Plan]:
        """Все невыполненные сроки, по времени возрастания."""
        sql = ("SELECT * FROM plans WHERE fired_at IS NULL")
        args: tuple = ()
        if plugin is not None:
            sql += " AND plugin = ?"
            args = (plugin,)
        sql += " ORDER BY due_at"
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        return [_plan(row) for row in rows]

    def get(self, plan_id: str) -> Plan | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM plans WHERE id=?",
                              (plan_id,)).fetchone()
        return _plan(row) if row else None

    def mark_fired(self, plan_id: str) -> None:
        """Отметить выполненным.

        Отметка нужна, а не удаление: иначе после перезапуска нельзя
        понять, сработало ли уже. Запись без отметки выстрелила бы
        повторно.
        """
        with self._connect() as con:
            con.execute("UPDATE plans SET fired_at=? WHERE id=?",
                        (time.time(), plan_id))

    def cancel(self, plan_id: str) -> bool:
        """Снять срок. `False` — такого не было."""
        with self._connect() as con:
            cur = con.execute("DELETE FROM plans WHERE id=? AND fired_at IS NULL",
                              (plan_id,))
            return cur.rowcount > 0

    def purge_fired(self, older_than_s: float = 7 * 24 * 3600) -> int:
        """Убрать давно отработавшие записи."""
        with self._connect() as con:
            cur = con.execute(
                "DELETE FROM plans WHERE fired_at IS NOT NULL"
                " AND fired_at < ?", (time.time() - older_than_s,))
            return cur.rowcount


def _plan(row: sqlite3.Row) -> Plan:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    return Plan(id=row["id"], plugin=row["plugin"], due_at=row["due_at"],
                payload=payload if isinstance(payload, dict) else {},
                created_at=row["created_at"], fired_at=row["fired_at"])


class Scheduler:
    """Будит плагины в срок и переживает перезапуск.

    Живёт в ядре потому, что только ядро знает, когда поднялся
    ассистент: задача, созданная до перезапуска, не пережила бы его, а
    задача, созданная после, не узнала бы, что человек ждёт звонка
    с прошлого раза.
    """

    def __init__(self, store: PlanStore, fires: Fires | None = None) -> None:
        self.store = store
        self._fires: Fires = dict(fires or {})
        self._tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- регистрация вызовов ------------------------------------------

    def register_fire(self, plugin: str, callback) -> None:
        """Назначить плагину обработчик его срабатывания."""
        self._fires[plugin] = callback

    def knows(self, plugin: str) -> bool:
        return plugin in self._fires

    # --- постановка -----------------------------------------------------

    async def schedule(self, plan_id: str, plugin: str, delay_s: float,
                       payload: dict | None = None) -> Plan:
        """Поставить срок и взвести часы."""
        if delay_s < 0:
            raise ValueError("срок в прошлом — так не ставят")
        plan = self.store.add(plan_id, plugin, time.time() + delay_s, payload)
        self._arm(plan)
        return plan

    def schedule_nowait(self, plan_id: str, plugin: str, delay_s: float,
                        payload: dict | None = None) -> Plan:
        """Поставить срок из кода, который не ждёт (протокол плагина).

        Отдельный метод, а не `schedule`, потому что там `async`: вызов из
        обработки протокола уже идёт внутри работающего цикла, и ждать
        нечего. Если планировщик не запущен, срок всё равно ложится в
        базу — его поднимет следующий старт, — но об этом честно
        пишется в журнал.
        """
        plan = self.store.add(plan_id, plugin, time.time() + max(0.0, delay_s),
                              payload)
        self._arm(plan)
        return plan

    def schedule_at(self, plan_id: str, plugin: str, due_at: float,
                    payload: dict | None = None) -> Plan:
        """Поставить срок на конкретный момент."""
        plan = self.store.add(plan_id, plugin, due_at, payload)
        self._arm(plan)
        return plan

    def cancel(self, plan_id: str) -> bool:
        """Снять срок и остановить часы."""
        task = self._tasks.pop(plan_id, None)
        if task is not None and not task.done():
            task.cancel()
        return self.store.cancel(plan_id)

    def cancel_all(self, plugin: str) -> int:
        """Снять все сроки плагина. Вызывается при его выключении."""
        removed = 0
        for plan in self.store.pending(plugin):
            if self.cancel(plan.id):
                removed += 1
        return removed

    # --- ход часов ------------------------------------------------------

    def _arm(self, plan: Plan) -> None:
        task = self._tasks.get(plan.id)
        if task is not None and not task.done():
            task.cancel()
            self._tasks.pop(plan.id, None)

        loop = self._loop
        if loop is None or loop.is_closed():
            # Явно не поднимали расписание — берём цикл, который
            # работает прямо сейчас. Раньше здесь был молчаливый выход:
            # срок записывался, задача не создавалась, и человек
            # ждал звонка, которого не будет.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._loop = loop

        if loop is None:
            log.warning("Срок %s сохранён, но ждать некому: нет цикла "
                        "событий. Сработает при следующем запуске.", plan.id)
            return

        self._tasks[plan.id] = loop.create_task(
            self._wait_and_fire(plan), name=f"sinaps-plan-{plan.id}")

    async def _wait_and_fire(self, plan: Plan) -> None:
        try:
            await asyncio.sleep(plan.due_in())
        except asyncio.CancelledError:
            return
        await self.fire(plan)

    async def fire(self, plan: Plan) -> bool:
        """Позвать плагин. `False` — некому звать.

        Плагин мог быть выключен, пока срок висел. Тогда запись
        отмечается выполненной и **стирается**: иначе она будет
        бить в пустоту при каждом запуске, а человек так и не узнает,
        что его забыли.
        """
        callback = self._fires.get(plan.plugin)
        if callback is None:
            log.info("Срок %s сгорел: плагин %s выключен",
                     plan.id, plan.plugin)
            self.store.mark_fired(plan.id)
            self._tasks.pop(plan.id, None)
            return False

        try:
            await callback(plan.payload)
        except asyncio.CancelledError:
            raise
        except Exception:                             # noqa: BLE001
            # Ошибка плагина не должна ронять ассистента и не должна
            # оставлять срок висеть навсегда: он повторно ударит при
            # следующем запуске.
            log.exception("Плагин %s не обработал срок %s",
                          plan.plugin, plan.id)
            self.store.mark_fired(plan.id)
            self._tasks.pop(plan.id, None)
            return False

        self.store.mark_fired(plan.id)
        self._tasks.pop(plan.id, None)
        return True

    # --- старт и остановка ----------------------------------------------

    def restore(self) -> list[Plan]:
        """Взвести все невыполненные сроки при старте.

        Просроченные не отбрасываются: человек просил напомнить в три
        часа и вернулся в пять — он всё ещё должен это услышать.
        """
        plans = self.store.pending()
        for plan in plans:
            self._arm(plan)
        if plans:
            overdue = [p for p in plans if p.due_in() == 0.0]
            log.info("Восстановлено сроков: %d, из них просроченных: %d",
                     len(plans), len(overdue))
        return plans

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Запомнить цикл и взвести сроки."""
        self._loop = loop or asyncio.get_event_loop()
        self.restore()

    async def stop(self) -> None:
        """Остановить часы. Сроки остаются в файле — их поднимет
        следующий запуск."""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    def pending_ids(self) -> list[str]:
        return [p.id for p in self.store.pending()]