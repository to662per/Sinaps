# -*- coding: utf-8 -*-
"""Таймер срабатывает по-настоящему: плагин, ядро, уведомление.

Прежние тесты проверяли куски по отдельности — разбор времени, план
в базе, разбор звонка. Между кусками был зазор, и в нём стоял `NameError`:
при звонке раннер обращался к `module`, которого в этой области не
было, и падал. Плагин умирал, голоса не было, а все тесты были
зелёные.

Этот тест проходит весь путь целиком: плагин ставит срок через ядро,
ядро будит его, плагин зовёт `notify`, и уведомление доходит до
обработчика приложения.
"""

from __future__ import annotations

import asyncio

import pytest

PLUGIN_YAML = """name: bell
version: 1.0.0
description: Звоночек
entry: plugin:register
permissions:
  - notify
  - schedule
"""

PLUGIN_PY = '''"""Плагин, который ставит срок и звенит."""

from sinaps.skills.base import Response

_CONTEXT = None


async def _set(ctx, slots):
    seconds = float(slots.get("seconds") or 1)
    await ctx.schedule("bell-1", seconds, {"why": "звонок"})
    return Response(speech="поставил")


async def _on_fire(ctx, slots):
    why = (slots or {}).get("why") or "звонок"
    return Response(speech=why)


async def _left(ctx, slots):
    left = await ctx.pending()
    seconds = int(left.get("bell-1", 0)) if isinstance(left, dict) else 0
    return Response(speech=f"осталось {seconds} секунд")


def register(registry):
    global _CONTEXT
    from sinaps.skills.base import Skill, SlotSpec

    _CONTEXT = getattr(registry, "context", None)

    skill = Skill("bell", description="Звоночек")
    skill.command("on_fire", _on_fire, patterns=[r"(?!)"], examples=[],
                  description="Внутренняя команда: ядро зовёт в срок.")
    skill.command("left", _left, patterns=[r"(?!)"], examples=[],
                  description="Сколько осталось.")
    skill.command("set", _set,
                  patterns=[r"^звоночек\s+(?P<seconds>\\d+)$"],
                  slots={"seconds": SlotSpec(name="seconds", type="number",
                                             required=True,
                                             description="Через сколько")},
                  examples=["звоночек 1"],
                  description="Поставить звонок.")
    registry.add(skill)
'''


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    """Файл одобрений — свой, в tmp_path.

    Права плагину даёт человек, и по умолчанию плагин не получает
    ничего. Без одобрения тест проверял бы отказ, а не работу.
    """
    from sinaps.plugins import approvals as approvals_module

    target = tmp_path / "approvals.json"
    monkeypatch.setattr(approvals_module, "path", lambda: target)
    approvals_module.grant("bell", ("notify", "schedule"))
    return target


@pytest.fixture
def plugin_dir(tmp_path):
    folder = tmp_path / "bell"
    folder.mkdir()
    (folder / "manifest.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
    (folder / "plugin.py").write_text(PLUGIN_PY, encoding="utf-8")
    return folder


@pytest.mark.asyncio
async def test_timer_really_rings(plugin_dir, tmp_path, approvals):
    """Полный круг: команда → срок → звонок → уведомление."""
    from sinaps.core.scheduler import PlanStore, Scheduler
    from sinaps.plugins.loader import load_plugin
    from sinaps.skills.registry import SkillRegistry

    shown: list[tuple[str, str]] = []

    scheduler = Scheduler(PlanStore(tmp_path / "plans.db"))
    registry = SkillRegistry()

    loaded = await load_plugin(plugin_dir, registry, scheduler=scheduler,
                               notify_handler=lambda t, x: shown.append((t, x)))
    try:
        scheduler.start(asyncio.get_running_loop())

        # Плагин ставит срок через ядро.
        reply = await loaded.process.call("bell.set", {}, {"seconds": 0.1})
        assert reply is not None, f"плагин не ответил: {reply}"

        assert scheduler.pending_ids() == ["bell-1"], (
            "срок не дошёл до ядра — таймер не поставлен")

        # Ждём, пока ядро разбудит плагин.
        for _ in range(100):
            await asyncio.sleep(0.05)
            if shown:
                break

        assert shown, "звонок не дошёл до приложения"
        assert shown[0][1] == "звонок", shown
    finally:
        await scheduler.stop()
        await loaded.process.stop()


@pytest.mark.asyncio
async def test_fire_does_not_kill_the_plugin(plugin_dir, tmp_path,
                                                approvals):
    """После звонка плагин жив.

    Раньше он падал с `NameError` прямо на звонке: имя модуля было
    не в той области. Плагин умирал, и следующий срок остался без
    звонка — а человек об этом не узнавал.
    """
    from sinaps.core.scheduler import PlanStore, Scheduler
    from sinaps.plugins.loader import load_plugin
    from sinaps.skills.registry import SkillRegistry

    shown: list[tuple[str, str]] = []
    scheduler = Scheduler(PlanStore(tmp_path / "plans.db"))
    registry = SkillRegistry()

    loaded = await load_plugin(plugin_dir, registry, scheduler=scheduler,
                               notify_handler=lambda t, x: shown.append((t, x)))
    try:
        scheduler.start(asyncio.get_running_loop())
        await loaded.process.call("bell.set", {}, {"seconds": 0.1})
        for _ in range(100):
            await asyncio.sleep(0.05)
            if shown:
                break

        # Плагин должен остаться в живых и отвечать.
        reply = await loaded.process.call("bell.set", {}, {"seconds": 5})
        assert reply is not None, "плагин перестал отвечать после звонка"
    finally:
        await scheduler.stop()
        await loaded.process.stop()

@pytest.mark.asyncio
async def test_remaining_time_is_visible(plugin_dir, tmp_path, approvals):
    """«Сколько осталось» отвечает по-настоящему.

    Человек спрашивает вслух, чтобы понять, стоит ли ждать. Ответ
    должен прийти из того же расписания, где стоял срок, — иначе
    «осталось пять минут» не совпадёт с тем, что сработает.
    """
    from sinaps.core.scheduler import PlanStore, Scheduler
    from sinaps.plugins.loader import load_plugin
    from sinaps.skills.registry import SkillRegistry

    scheduler = Scheduler(PlanStore(tmp_path / "plans.db"))
    registry = SkillRegistry()
    shown: list[tuple[str, str]] = []

    loaded = await load_plugin(plugin_dir, registry, scheduler=scheduler,
                               notify_handler=lambda t, x: shown.append((t, x)))
    try:
        scheduler.start(asyncio.get_running_loop())
        await loaded.process.call("bell.set", {}, {"seconds": 300})

        reply = await loaded.process.call("bell.left", {})
        said = (reply.speech if reply is not None else "") or ""

        assert "300" in said, f"ответ не про остаток: {said!r}"
    finally:
        await scheduler.stop()
        await loaded.process.stop()
