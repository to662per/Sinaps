# -*- coding: utf-8 -*-
"""Напоминание по-настоящему: плагин, ядро, перезапуск, звонок.

Главное в напоминании — оно должно пережить перезапуск ассистента.
Именно это отличает его от таймера: между «поставь» и «напомни» может
пройти ночь, а ассистент за это время перезапустится.

Проверяется весь путь: плагин ставит срок в ядро, ядро поднимает его
после перезапуска, будит плагин, плагин отвечает, и уведомление доходит
до приложения.
"""

from __future__ import annotations

import asyncio

import pytest

PLUGIN_YAML = """name: remind
version: 1.0.0
description: Напоминание
entry: plugin:register
permissions:
  - notify
  - schedule
  - state
"""

PLUGIN_PY = '''"""Плагин-напоминание для проверки."""

from sinaps.skills.base import Response


async def _set(ctx, slots):
    text = str(slots.get("text") or "дело")
    await ctx.schedule("rem-1", float(slots.get("delay") or 1), {"text": text})
    return Response(speech="поставил")


async def _on_fire(ctx, slots):
    return Response(speech=str((slots or {}).get("text") or "пора"))


def register(registry):
    from sinaps.skills.base import Skill, SlotSpec

    skill = Skill("remind", description="Напоминание")
    skill.command("set", _set,
                  patterns=[r"^напомни\\s+(?P<text>.+)$"],
                  slots={"text": SlotSpec(name="text", type="string",
                                          required=True, description="Что"),
                         "delay": SlotSpec(name="delay", type="number",
                                           required=False,
                                           description="Через сколько")},
                  examples=["напомни дело"], description="Поставить.")
    skill.command("on_fire", _on_fire, patterns=[r"(?!)"], examples=[],
                  description="Внутренняя.")
    registry.add(skill)
'''


@pytest.fixture
def plugin_dir(tmp_path):
    folder = tmp_path / "remind"
    folder.mkdir()
    (folder / "manifest.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
    (folder / "plugin.py").write_text(PLUGIN_PY, encoding="utf-8")
    return folder


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    """Свой файл одобрений: права плагину даёт человек."""
    from sinaps.plugins import approvals as approvals_module

    target = tmp_path / "approvals.json"
    monkeypatch.setattr(approvals_module, "path", lambda: target)
    approvals_module.grant("remind", ("notify", "schedule", "state"))
    return target


@pytest.mark.asyncio
async def test_reminder_survives_restart(plugin_dir, tmp_path, approvals):
    """Поставил, перезапустил ассистент — напоминание всё равно звенит.

    Между постановкой и звонком может пройти ночь, а ассистент за это
    время перезапустится. Напоминание, которое теряется при перезапуске,
    хуже бесполезного: человек ждёт и не получает звонка.
    """
    from sinaps.core.scheduler import PlanStore, Scheduler
    from sinaps.plugins.loader import load_plugin
    from sinaps.skills.registry import SkillRegistry

    path = tmp_path / "plans.db"
    scheduler = Scheduler(PlanStore(path))
    registry = SkillRegistry()
    shown: list[tuple[str, str]] = []

    loaded = await load_plugin(plugin_dir, registry, scheduler=scheduler,
                               notify_handler=lambda t, x: shown.append((t, x)))
    try:
        scheduler.start(asyncio.get_running_loop())
        await loaded.process.call("remind.set", {}, {"text": "позвонить маме",
                                                     "delay": 0.2})
        assert scheduler.pending_ids(), "напоминание не попало в расписание"
    finally:
        await scheduler.stop()
        await loaded.process.stop()

    # «Перезапуск»: новое хранилище, новый планировщик, новый плагин.
    seen: list[tuple[str, str]] = []
    scheduler2 = Scheduler(PlanStore(path))
    registry2 = SkillRegistry()
    loaded2 = await load_plugin(plugin_dir, registry2, scheduler=scheduler2,
                                notify_handler=lambda t, x: seen.append((t, x)))
    try:
        scheduler2.start(asyncio.get_running_loop())
        assert scheduler2.pending_ids(), "напоминание не пережило перезапуск"

        for _ in range(100):
            await asyncio.sleep(0.05)
            if seen:
                break

        assert seen, "после перезапуска напоминание не позвонило"
        assert seen[0][1] == "позвонить маме", seen
    finally:
        await scheduler2.stop()
        await loaded2.process.stop()
