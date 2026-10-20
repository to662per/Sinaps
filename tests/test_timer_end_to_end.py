# -*- coding: utf-8 -*-
"""Таймер работает по всей цепочке, а не по кускам.

Собранный ассистент должен поднять расписание, отдать его плагинам и
погасить при остановке. Проверяется здесь, потому что каждый кусок
по отдельности проходит, а между ними срок может потеряться — и человек
увидит тишину вместо звонка.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


def test_build_passes_the_scheduler():
    """Сборка отдаёт планировщик загрузчику плагинов.

    Планировщик без плагинов или плагины без планировщика — срок
    поставится в базу, а звонок не придёт.
    """
    from sinaps.runtime import build_skills_runtime

    source = inspect.getsource(build_skills_runtime)

    assert "scheduler=scheduler" in source, (
        "сборка не отдаёт планировщик плагинам — сроки не сработают")
    assert "build_scheduler()" in source, (
        "сборка не создаёт планировщик, когда его не дали")


def test_build_creates_a_scheduler(tmp_path):
    """Планировщик создаётся и кладёт сроки в свой файл.

    Отдельный файл, а не таблица в базе истории: очистка истории не
    должна стирать напоминания.
    """
    from sinaps.runtime import build_scheduler

    scheduler = build_scheduler(tmp_path)

    assert scheduler.store.path == tmp_path / "plans.db"
    assert scheduler.store.pending() == []


def test_runtime_keeps_the_scheduler():
    """Рантайм помнит планировщик: он его поднимает и гасит."""
    from sinaps.runtime import Runtime

    assert hasattr(Runtime, "scheduler")


def test_runtime_starts_and_stops_the_scheduler():
    """Подъём и остановка расписания — в начале и в конце жизни.

    Подъём после загрузки плагинов: просроченный срок может
    сработать сразу же, и обработчики звонков должны быть уже на
    месте.
    """
    from sinaps.runtime import Runtime

    start = inspect.getsource(Runtime.start)
    stop = inspect.getsource(Runtime.stop)

    assert "scheduler.start" in start, "расписание не поднимается"
    assert "scheduler.stop" in stop, "расписание не гасится"


def test_start_calls_scheduler_after_plugins():
    """Расписание поднимается после загрузки плагинов.

    Иначе просроченный срок выстрелит раньше, чем плагин зарегистрирует
    свой обработчик, и звонок уйдёт в никуда.
    """
    from sinaps.runtime import Runtime

    source = inspect.getsource(Runtime.start)

    plugins_at = source.find("load_plugins") if "load_plugins" in source else -1
    scheduler_at = source.find("scheduler.start")

    assert scheduler_at > 0, "расписание не поднимается"
    if plugins_at > 0:
        assert scheduler_at > plugins_at, (
            "расписание поднимается раньше плагинов — звонок уйдёт в никуда")


@pytest.mark.asyncio
async def test_scheduler_receives_a_plan_and_fires_it(tmp_path):
    """Срок, поставленный через ядро, доходит до плагина.

    Полный круг на настоящем планировщике: запись, ожидание, звонок,
    отметка. Именно этот круг человек и наблюдает.
    """
    from sinaps.core.scheduler import PlanStore, Scheduler

    seen: list[dict] = []

    async def _fire(plan):
        seen.append(plan.payload)

    sched = Scheduler(PlanStore(tmp_path / "plans.db"), {"timer": _fire})
    sched.start(asyncio.get_running_loop())

    await sched.schedule("timer-1", "timer", 0.05, {"why": "Пора"})
    await asyncio.sleep(0.2)

    assert seen == [{"why": "Пора"}], "плагина не позвали"
    assert sched.pending_ids() == [], "срок не отмечен выполненным"

    await sched.stop()


@pytest.mark.asyncio
async def test_plan_survives_assistant_restart(tmp_path):
    """Поставил, перезапустил ассистент — звонок всё равно пришёл."""
    from sinaps.core.scheduler import PlanStore, Scheduler

    path = tmp_path / "plans.db"

    first = Scheduler(PlanStore(path), {"timer": _noop})
    first.start(asyncio.get_running_loop())
    first.schedule_nowait("timer-1", "timer", 0.05, {"why": "Пора"})
    await first.stop()

    seen: list[dict] = []

    async def _fire(plan):
        seen.append(plan.payload)

    second = Scheduler(PlanStore(path), {"timer": _fire})
    second.start(asyncio.get_running_loop())
    await asyncio.sleep(0.2)

    assert seen == [{"why": "Пора"}], "после перезапуска срок не сработало"

    await second.stop()


async def _noop(_payload: dict) -> None:
    return None