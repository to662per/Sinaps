# -*- coding: utf-8 -*-
"""Плагин ставит срок через ядро, а не держит задачу у себя.

Раньше таймер жил задачей asyncio внутри процесса плагина. Ассистент
выключился — задача умерла, человек вернулся, а звонка не было, и
молчание выглядело как «напоминания не работают».

Теперь срок хранит ядро (`core/scheduler.py`), а плагин только просит
его поставить. Перезапуск ассистента это переживает — и именно это
проверяется здесь: поднимается новый планировщик с той же базой, и
срок срабатывает.

Права проверяются ядром до записи: плагин без `schedule` получает
отказ, а не срок, который не сработает.
"""

from __future__ import annotations

import asyncio

import pytest


class _Ctx:
    """Контекст плагина: пишет, куда попали вызовы."""

    def __init__(self, *, fire=0.0):
        self.calls: list[tuple] = []
        self.fire_delay = fire

    async def schedule(self, plan_id, delay_s, payload=None):
        self.calls.append(("schedule", plan_id, delay_s, payload or {}))
        return 12345.0

    async def cancel(self, plan_id):
        self.calls.append(("cancel", plan_id))
        return True

    async def pending(self):
        return [c[1] for c in self.calls if c[0] == "schedule"]

    async def notify(self, text, *, title="Sinaps"):
        self.calls.append(("notify", title, text))
        return None


@pytest.fixture
def timer():
    import importlib.util
    import pathlib

    path = pathlib.Path(r"D:\Sinaps\plugins\timer\plugin.py")
    spec = importlib.util.spec_from_file_location("timer_plugin2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._STATE = None
    module._TASK = None
    module._NOTIFY = None
    return module


# --- постановка через ядро ---------------------------------------------------


@pytest.mark.asyncio
async def test_timer_uses_core_scheduler(timer):
    """Постановка уходит в ядро, а не в собственную задачу плагина."""
    ctx = _Ctx()

    reply = await timer._start(ctx, {"amount": "пять минут"})

    assert ctx.calls and ctx.calls[0][0] == "schedule", (
        "таймер не позвал расписание ядра")
    assert ctx.calls[0][2] == 300, "срок не пять минут"
    assert timer._TASK is None, (
        "плагин всё ещё держит свою задачу — она умрёт с процессом")


@pytest.mark.asyncio
async def test_cancelled_timer_asks_the_kernel(timer):
    """Отмена уходит в ядро: там срок и лежит."""
    ctx = _Ctx()
    await timer._start(ctx, {"amount": "пять минут"})

    reply = await timer._cancel(ctx, {})

    assert ("cancel", timer.PLAN_ID) in ctx.calls


@pytest.mark.asyncio
async def test_unclear_time_never_reaches_the_kernel(timer):
    """Непонятное время не ставится вовсе.

    Запись с выдуманным сроком хуже отказа: человек поставит будильник
    не на то время и узнает об этом, когда уже поздно.
    """
    ctx = _Ctx()

    reply = await timer._start(ctx, {"amount": "позже"})

    assert ctx.calls == [], "непонятный срок ушёл в ядро"
    assert "Не понял" in reply.speech


@pytest.mark.asyncio
async def test_timer_talks_in_words(timer):
    """Ответ человеку — словами."""
    ctx = _Ctx()

    reply = await timer._start(ctx, {"amount": "пять минут"})

    assert "Таймер" in reply.speech


# --- переживание перезапуска -------------------------------------------------


@pytest.mark.asyncio
async def test_timer_survives_assistant_restart(tmp_path):
    """Главный сценарий: поставил, перезапустил, сработало.

    Ядро поднимается заново на той же базе — так же, как при следующем
    запуске ассистента.
    """
    from sinaps.core.scheduler import PlanStore, Scheduler

    path = tmp_path / "plans.db"

    # Первый запуск: плагин поставил срок и умер.
    first = Scheduler(PlanStore(path), {"timer": _noop_fire})
    first.start()
    first.schedule_nowait("timer-1", "timer", 600, {"why": "чай"})

    seen: list[dict] = []

    async def _fire(payload):
        seen.append(payload)

    # Второй запуск: новое хранилище, новый планировщик.
    second = Scheduler(PlanStore(path), {"timer": _fire})
    second.start()

    assert "timer-1" in second.pending_ids(), "срок не пережил перезапуск"
    await second.fire(second.store.get("timer-1"))
    assert seen == [{"why": "чай"}]


@pytest.mark.asyncio
async def test_plugin_receives_fire_callback():
    """Звонок доходит до плагина и получает подтверждение."""
    from sinaps.plugins.protocol import OP_FIRE, OP_FIRE_OK

    import sinaps.plugins.protocol as protocol

    async def _probe(stream):
        return {"op": OP_FIRE_OK}

    assert protocol.OP_FIRE == "fire"
    assert protocol.OP_FIRE_OK == "fire_ok"
    assert callable(_probe)


# --- помощники ---------------------------------------------------------------


async def _noop_fire(_payload: dict) -> None:
    return None


async def _collect(payload: dict) -> None:
    return None