# -*- coding: utf-8 -*-
"""Плагин в загрузчике получает расписание и регистрирует свой звонок.

Проверяется на настоящем плагине из временной папки, а не по исходнику
загрузчика: исходник может содержать нужное слово и при этом ничего не
делать. Именно так и было — ветка существовала, а планировщик до
плагина не доходил.
"""

from __future__ import annotations

import pytest

PLUGIN_YAML = """name: probe
version: 1.0.0
description: Пробный плагин
entry: plugin:register
permissions:
  - notify
  - schedule
"""

PLUGIN_PY = '''"""Пробный плагин."""


def register(registry) -> None:
    from sinaps.skills.base import Skill

    skill = Skill("probe", description="Проба")
    skill.command("ping", _ping, patterns=[r"^пинг$"],
                  examples=["пинг"], description="Ответить")
    registry.add(skill)


async def _ping(ctx, slots):
    from sinaps.skills.base import Response

    return Response(speech="пинг")


async def on_fire(payload):
    return None
'''


@pytest.fixture
def plugin_dir(tmp_path):
    folder = tmp_path / "probe"
    folder.mkdir()
    (folder / "manifest.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
    (folder / "plugin.py").write_text(PLUGIN_PY, encoding="utf-8")
    return folder


@pytest.mark.asyncio
async def test_plugin_gets_the_scheduler(plugin_dir, tmp_path):
    """Загруженный плагин получает расписание ядра.

    Без него плагин не поставит срок: `_schedule_action` ответит
    «расписание не запущено», и человек услышит отказ вместо
    подтверждения.
    """
    from sinaps.core.scheduler import PlanStore, Scheduler
    from sinaps.plugins.loader import load_plugin
    from sinaps.skills.registry import SkillRegistry

    scheduler = Scheduler(PlanStore(tmp_path / "plans.db"))
    registry = SkillRegistry()

    loaded = await load_plugin(plugin_dir, registry, scheduler=scheduler)
    try:
        assert loaded.process._scheduler is scheduler, (
            "плагин не получил расписание — срок не поставится")
    finally:
        # Планировщик гасится явно: незакрытая задача переживёт цикл и
        # уедет в следующий тест, где повиснет всё.
        await scheduler.stop()
        await loaded.process.stop()


@pytest.mark.asyncio
async def test_plugin_registers_its_fire_handler(plugin_dir, tmp_path):
    """Плагин регистрирует, кто его будит.

    Планировщик знает только имена. Если плагин не зарегистрировал
    свой обработчик, срок сгорит в никуда: ядро не знает, кого звать.
    """
    from sinaps.core.scheduler import PlanStore, Scheduler
    from sinaps.plugins.loader import load_plugin
    from sinaps.skills.registry import SkillRegistry

    scheduler = Scheduler(PlanStore(tmp_path / "plans.db"))
    registry = SkillRegistry()

    loaded = await load_plugin(plugin_dir, registry, scheduler=scheduler)
    try:
        assert scheduler.knows("probe"), (
            "плагин не зарегистрировал свой звонок — срок сгорит в никуда")
        assert scheduler._fires["probe"] == loaded.process.fire_plan
    finally:
        await scheduler.stop()
        for plugin in list(getattr(locals().get("report"), "loaded", []) or []):
            try:
                await plugin.process.stop()
            except Exception:
                pass
        await loaded.process.stop()


@pytest.mark.asyncio
async def test_plugin_without_scheduler_gets_a_refusal(plugin_dir):
    """Без расписания плагин получает отказ, а не молчание.

    Отказ виден: плагин сказа�� человеку, что напоминание поставить не
    вышло. Молчание выглядело бы как «поставил», а звонка не было бы.
    """
    from sinaps.plugins.loader import load_plugin
    from sinaps.skills.registry import SkillRegistry

    registry = SkillRegistry()
    loaded = await load_plugin(plugin_dir, registry)
    try:
        assert loaded.process._scheduler is None
        with pytest.raises(Exception):
            loaded.process._schedule_action("at", {"plan_id": "t1",
                                                   "delay_s": 60})
    finally:
        await loaded.process.stop()


@pytest.mark.asyncio
async def test_load_all_gives_every_plugin_the_scheduler(plugin_dir, tmp_path):
    """Загрузка папки целиком тоже раздаёт расписание.

    `load_all` — путь, которым ассистент грузит плагины на самом деле.
    Если он не передаст планировщик, точка входа работать не будет,
    а точечная проверка `load_plugin` этого не покажет.
    """
    from sinaps.core.scheduler import PlanStore, Scheduler
    from sinaps.plugins.loader import load_all
    from sinaps.skills.registry import SkillRegistry

    scheduler = Scheduler(PlanStore(tmp_path / "plans.db"))
    registry = SkillRegistry()

    report = await load_all(plugin_dir.parent, registry, scheduler=scheduler)
    try:
        assert report.loaded, "плагин не загрузился"
        assert scheduler.knows("probe"), (
            "загрузка папкой не раздала расписание — звонок не придёт")
    finally:
        await scheduler.stop()
        for plugin in list(getattr(locals().get("report"), "loaded", []) or []):
            try:
                await plugin.process.stop()
            except Exception:
                pass
