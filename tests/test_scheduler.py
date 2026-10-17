# -*- coding: utf-8 -*-
"""Расписание переживает перезапуск — это и проверяется здесь.

Главный обещанный человеком сценарий: поставить напоминание, закрыть
ассистент, открыть снова — и получить звонок. Всё остальное — детали,
из-за которых этот сценарий обычно и ломается.
"""

from __future__ import annotations

import time

import pytest

from sinaps.core.scheduler import PlanStore, Scheduler


@pytest.fixture
def store(tmp_path):
    return PlanStore(tmp_path / "plans.db")


# --- хранилище ---------------------------------------------------------------


def test_plan_is_stored(store):
    plan = store.add("t1", "timer", time.time() + 300, {"why": "чай"})
    assert store.get("t1") is not None
    assert store.get("t1").payload == {"why": "чай"}
    assert plan.pending


def test_plans_survive_reopening(tmp_path):
    """Файл сроков переживает закрытие базы.

    Без этого «переживает перезапуск» — просто слова: срок держался бы
    в памяти процесса и пропал бы вместе с ним.
    """
    path = tmp_path / "plans.db"

    first = PlanStore(path)
    first.add("t1", "timer", time.time() + 300)

    second = PlanStore(path)
    assert [p.id for p in second.pending()] == ["t1"]


def test_re_scheduling_replaces_not_duplicates(store):
    """Повтор с тем же именем переписывает запись.

    Человек сказал «отмени» и тут же «поставь» — отказ из-за висящей
    записи был бы нелепостью.
    """
    store.add("t1", "timer", time.time() + 300)
    store.add("t1", "timer", time.time() + 900)

    pending = store.pending("timer")
    assert len(pending) == 1
    assert pending[0].due_at > time.time() + 600


def test_fired_plan_does_not_fire_again(store):
    """Отмеченный срок не срабатывает повторно."""
    store.add("t1", "timer", time.time() + 300)
    store.mark_fired("t1")

    assert store.pending() == []
    assert store.get("t1").fired_at is not None


def test_cancel_removes_only_pending(store):
    store.add("t1", "timer", time.time() + 300)

    assert store.cancel("t1") is True
    assert store.cancel("t1") is False, "отмена отсутствующего сказала «да»"


def test_plans_of_other_plugins_are_untouched(store):
    """Снятие чужих сроков — нельзя."""
    store.add("t1", "timer", time.time() + 300)
    store.add("n1", "notes", time.time() + 300)

    assert [p.id for p in store.pending("notes")] == ["n1"]
    store.cancel("n1")
    assert [p.id for p in store.pending("timer")] == ["t1"]


def test_broken_payload_does_not_break_reading(store):
    """Битая полезная нагрузка не роняет чтение.

    Строка в базе может оказаться чем угодно: ручная правка, обрыв
    записи. Плагин получит пустой словарь, а не исключение на старте.
    """
    store.add("t1", "timer", time.time() + 300, {"ok": 1})
    import sqlite3

    with sqlite3.connect(store.path) as con:
        con.execute("UPDATE plans SET payload='{не json' WHERE id='t1'")

    plan = store.get("t1")
    assert plan is not None and plan.payload == {}


# --- ход часов ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_fires_after_delay(store):
    """Срок срабатывает и зовёт плагин."""
    seen: list[dict] = []
    sched = Scheduler(store, {"timer": lambda p: _collect(seen, p)})

    await sched.schedule("t1", "timer", 0.05, {"why": "чай"})
    await asyncio_sleep(0.2)

    assert seen == [{"why": "чай"}], "плагин не позвали"
    assert store.get("t1").fired_at is not None


@pytest.mark.asyncio
async def test_missing_plugin_does_not_block_forever(store):
    """Плагина нет — запись гаснет, а не висит вечно.

    Иначе она била бы в пустоту при каждом запуске, и человек не
    узнал бы, что его забыли.
    """
    store.add("t1", "timer", time.time() - 1)
    sched = Scheduler(store)          # плагин не зарегистрирован
    sched.start()

    assert await sched.fire(store.pending()[0]) is False
    assert store.pending() == []


@pytest.mark.asyncio
async def test_failing_plugin_does_not_break_assistant(store):
    """Падение плагина не роняет ассистента и не оставляет срок висеть."""
    async def _boom(_payload):
        raise RuntimeError("плагин сломался")

    sched = Scheduler(store, {"timer": _boom})
    plan = await sched.schedule("t1", "timer", 0.01)

    assert await sched.fire(plan) is False
    assert store.pending() == [], "срок остался и выстрелит снова"


@pytest.mark.asyncio
async def test_cancel_stops_the_clock(store):
    """Отмена снимает и запись, и задачу."""
    sched = Scheduler(store, {"timer": lambda p: _noop()})
    sched.start()
    await sched.schedule("t1", "timer", 60)

    assert sched.cancel("t1") is True
    assert sched.pending_ids() == []


@pytest.mark.asyncio
async def test_cancelled_plan_does_not_fire(store):
    """Отменённый срок не выстреливает.

    Запись стирается, но задача в памяти остаётся: если её не снять,
    таймер позовёт плагина спустя срок после того, как человек
    сказал «отмени». Это хуже, чем вообще не завести таймер —
    человек уверит его, что тот сработает.
    """
    seen: list[dict] = []
    sched = Scheduler(store, {"timer": lambda p: _collect(seen, p)})
    sched.start()

    await sched.schedule("t1", "timer", 0.05, {"why": "чай"})
    assert sched.cancel("t1") is True

    await asyncio_sleep(0.2)

    assert seen == [], "отменённый таймер всё равно позвал плагина"


# --- перезапуск --------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_survives_restart(tmp_path):
    """Главный сценарий: поставил, перезапустил, получил.

    Здесь поднимается новое хранилище и новый планировщик — как это
    делает ассистент при следующем запуске.
    """
    seen: list[dict] = []

    first = Scheduler(PlanStore(tmp_path / "plans.db"),
                      {"timer": lambda p: _collect(seen, p)})
    first.start()
    await first.schedule("t1", "timer", 0.05, {"why": "чай"})
    await asyncio_sleep(0.15)
    assert seen == [{"why": "чай"}]

    # Ставим второй срок и «перезапускаем» ассистент, не дожидаясь.
    await first.schedule("t2", "timer", 0.05, {"why": "второй"})
    await first.stop()

    seen.clear()
    second = Scheduler(PlanStore(tmp_path / "plans.db"),
                       {"timer": lambda p: _collect(seen, p)})
    second.start()
    assert "t2" in second.pending_ids(), "срок не пережил перезапуск"

    await asyncio_sleep(0.2)
    assert seen == [{"why": "второй"}], "после перезапуска не сработало"

    # Планировщик останавливается явно: незакрытая задача уезжает в
    # следующий тест и там подвешивает цикл, а виноват окажется не тот.
    await second.stop()


@pytest.mark.asyncio
async def test_overdue_plan_fires_right_after_restart(tmp_path):
    """Просроченный срок срабатывает сразу, а не пропадает.

    Человек просил напомнить в три часа, вернулся в пять. Если
    просроченное выбрасывалось, обещание было бы нарушено молча, и
    человек решил бы, что напоминания не работают.
    """
    path = tmp_path / "plans.db"
    store = PlanStore(path)
    store.add("old", "timer", time.time() - 3600, {"why": "вчера"})

    seen: list[dict] = []
    sched = Scheduler(PlanStore(path), {"timer": lambda p: _collect(seen, p)})
    sched.start()
    restored = sched.restore()

    assert [p.id for p in restored] == ["old"]
    await asyncio_sleep(0.15)
    assert seen == [{"why": "вчера"}]


@pytest.mark.asyncio
async def test_stop_keeps_plans_for_next_start(store):
    """Остановка не стирает сроки — их поднимет следующий запуск."""
    sched = Scheduler(store, {"timer": lambda p: _noop()})
    sched.start()
    await sched.schedule("t1", "timer", 600)

    await sched.stop()

    assert [p.id for p in store.pending()] == ["t1"]


# --- помощники ---------------------------------------------------------------


async def _collect(sink: list, payload: dict) -> None:
    sink.append(payload)


async def _noop() -> None:
    return None


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)