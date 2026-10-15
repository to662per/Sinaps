# -*- coding: utf-8 -*-
"""Горячая перезагрузка плагина без перезапуска ассистента.

Нужна автору плагина на этапе отладки: раньше правка кода требовала
перезапуска ассистента целиком, а это минуты на каждую итерацию.

Главная опасность здесь — не «не заработало», а «работает, но оставил
мусор»: старые команды в реестре, процесс, о котором забыли, или окно,
в котором плагин значится живым, а он уже нет. Все проверки ниже — про
это.
"""

from __future__ import annotations

import pytest

from sinaps.core.bus import EventBus, EventType
from sinaps.plugins import approvals
from sinaps.plugins.loader import (
    load_all, load_plugin, plugin_stats, reload_plugin, replace_plugin)
from sinaps.skills.registry import SkillRegistry


PLUGIN = '''\
from sinaps.plugins.api import Response, Skill

async def handle(ctx, slots):
    return Response(speech="{speech}")

def register(registry):
    skill = Skill("get")
    skill.command("now", handle, patterns=[r"^погода$"],
                  examples=["погода"], slots={})
    registry.add(skill)
    return skill
'''

MANIFEST = """\
name: {name}
version: 1.0.0
description: Тестовый плагин
entry: plugin:register
permissions:
  - state
"""


def make_plugin(folder, name="weather", speech="услышал"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(
        MANIFEST.format(name=name), encoding="utf-8")
    (folder / "plugin.py").write_text(
        PLUGIN.replace("{speech}", speech), encoding="utf-8")
    return folder


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry()


@pytest.fixture(autouse=True)
def isolated_approvals(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "path", lambda: tmp_path / "plugins.json")


class FakeCtx:
    def __init__(self, text="привет"):
        self.text = text
        self.source = "voice"
        self.confidence = 1.0
        self.history = []
        self.user_name = "Капитан"
        self.pending_intent = None


def find(registry: SkillRegistry, name: str):
    spec = next((s for s in registry.specs if s.name == name), None)
    if spec is None:
        raise AssertionError(
            f"нет команды «{name}»; зарегистрированы: "
            f"{sorted(registry.spec_names())}")
    return spec


async def say(registry: SkillRegistry, command: str, text: str = "привет"):
    return await find(registry, command).handler(FakeCtx(text), {})


# --- команды и реестр --------------------------------------------------------


async def test_reload_picks_up_new_code(tmp_path, registry):
    """После перезагрузки отвечает новая версия кода."""
    folder = make_plugin(tmp_path / "a", name="a", speech="старая версия")
    plugin = await load_plugin(folder, registry)
    try:
        assert (await say(registry, "a.get.now")).speech == "старая версия"

        make_plugin(folder, "a", speech="новая версия")
        fresh = await reload_plugin(plugin, folder, registry)

        assert (await say(registry, "a.get.now")).speech == "новая версия"
        assert fresh.commands == ("a.get.now",)
    finally:
        await plugin.stop()


async def test_reload_leaves_no_old_commands(tmp_path, registry):
    """Старые команды не остаются в реестре.

    Иначе после перезагрузки плагина с другим набором команд старые
    продолжали бы отвечать — и ассистент врал бы о своих возможностях.
    """
    folder = make_plugin(tmp_path / "a", name="a")
    plugin = await load_plugin(folder, registry)
    try:
        fresh = await reload_plugin(plugin, folder, registry)

        names = registry.spec_names()
        assert names.count("a.get.now") == 1, (
            f"команда продублировалась: {sorted(names)}")
        assert fresh.commands == ("a.get.now",)
    finally:
        await plugin.stop()


async def test_reload_uses_new_process(tmp_path, registry):
    """Перезагрузка поднимает новый процесс, а не будит старый.

    Иначе правка кода не имела бы смысла: старый процесс и новый код
    несовместимы.
    """
    folder = make_plugin(tmp_path / "a", name="a")
    plugin = await load_plugin(folder, registry)
    try:
        old_pid = plugin.process.pid
        old_process = plugin.process

        fresh = await reload_plugin(plugin, folder, registry)

        assert old_process is not old_process or fresh.process is not old_process
        assert fresh.process.pid != old_pid
        assert fresh.process.alive
    finally:
        await plugin.stop()


async def test_reload_reports_through_events(tmp_path, registry):
    """Перезагрузка видна как «уснул» и «поднялся»."""
    folder = make_plugin(tmp_path / "a", name="a")
    bus = EventBus()
    seen: list[tuple[str, str]] = []

    async def handler(event) -> None:  # noqa: ANN001
        if event.type in (EventType.PLUGIN_UNLOADED, EventType.PLUGIN_LOADED):
            seen.append((event.type.value, event.payload.get("plugin", "")))

    bus.on(EventType.PLUGIN_UNLOADED)(handler)
    bus.on(EventType.PLUGIN_LOADED)(handler)

    plugin = await load_plugin(folder, registry, bus=bus)
    try:
        await reload_plugin(plugin, folder, registry, bus=bus)

        assert ("plugin_loaded", "a") in seen, seen
        assert ("plugin_unloaded", "a") in seen, seen
    finally:
        await plugin.stop()


# --- отказы остаются отказы --------------------------------------------------


async def test_reload_failure_removes_commands(tmp_path, registry):
    """Неудачная перезагрузка убирает команды, а не оставляет мусор.

    Плагин после неудачи выключен, и это видно: его команд нет. Хуже
    было бы оставить старые команды от кода, которого больше нет.
    """
    folder = make_plugin(tmp_path / "a", name="a")
    plugin = await load_plugin(folder, registry)
    try:
        (folder / "plugin.py").write_text("import нет_такого\n",
                                          encoding="utf-8")

        with pytest.raises(Exception):
            await reload_plugin(plugin, folder, registry)

        assert "a.get.now" not in registry.spec_names(), (
            "команды сломанного плагина остались в реестре")
    finally:
        await plugin.stop()


async def test_reload_failure_reports_reason(tmp_path, registry):
    """Ошибка перезагрузки говорит, что именно сломалось."""
    folder = make_plugin(tmp_path / "a", name="a")
    plugin = await load_plugin(folder, registry)
    try:
        (folder / "plugin.py").write_text("import нет_такого\n",
                                          encoding="utf-8")

        with pytest.raises(Exception) as info:
            await reload_plugin(plugin, folder, registry)

        assert str(info.value), "ошибка без объяснения"
    finally:
        await plugin.stop()


async def test_reload_without_commands_is_error(tmp_path, registry):
    """Плагин без команд после перезагрузки — ошибка, а не тихий успех.

    Пустой набор команд означает, что код сломался настолько, что даже
    `register` ничего не объявил. Считать это успешной перезагрузкой
    нельзя: плагин выключен, но об этом никто не узнает.
    """
    folder = make_plugin(tmp_path / "a", name="a")
    plugin = await load_plugin(folder, registry)
    try:
        (folder / "plugin.py").write_text(
            "def register(registry):\n    return []\n", encoding="utf-8")

        with pytest.raises(Exception) as info:
            await reload_plugin(plugin, folder, registry)

        assert "команд" in str(info.value), str(info.value)
    finally:
        await plugin.stop()


# --- отчёт -------------------------------------------------------------------


async def test_replace_keeps_position(tmp_path, registry):
    """Позиция плагина в отчёте сохраняется.

    По списку в окне «Плагины» человек привыкает, где что стоит.
    Перезагрузка не должна отправлять карточку в конец.
    """
    for name in ("a", "b", "c"):
        make_plugin(tmp_path / name, name=name)

    report = await load_all(tmp_path, registry)
    try:
        assert [p.name for p in report.loaded] == ["a", "b", "c"]

        middle = report.loaded[1]
        fresh = await reload_plugin(middle, middle.process.folder,
                                    registry)
        replace_plugin(report, middle, fresh)

        assert [p.name for p in report.loaded] == ["a", "b", "c"]
    finally:
        await report.stop_all()


async def test_replace_by_name_when_object_differs(tmp_path, registry):
    """Замена срабатывает и по имени, если объект другой.

    После сбоя перезагрузки в отчёте может лежать не тот объект, что
    перезагружали. Поиск только по объекту его бы не нашёл, и плагин
    добавился бы вторым.
    """
    for name in ("a", "b"):
        make_plugin(tmp_path / name, name=name)

    report = await load_all(tmp_path, registry)
    try:
        original = report.loaded[0]
        impostor = type(original)(manifest=original.manifest)

        # Заменяем на тот же плагин: цель проверки — найти место по
        # имени, когда объект не тот. Проба «свежий» потребовала бы
        # сначала выгрузить исходный, а это уже другой сценарий.
        replace_plugin(report, impostor, original)

        assert [p.name for p in report.loaded] == ["a", "b"]
        assert report.loaded[0] is original
    finally:
        await report.stop_all()


# --- метрики после перезагрузки ---------------------------------------------


async def test_memory_not_grows_after_reload(tmp_path, registry):
    """После перезагрузки в памяти один процесс, а не два.

    Старый процесс должен быть закрыт, а не забыт. Если он остался
    висеть, каждый цикл отладки оставлял бы после себя ещё 4 МБ.
    """
    folder = make_plugin(tmp_path / "a", name="a")
    plugin = await load_plugin(folder, registry)
    report = type("R", (), {"loaded": [plugin]})()

    try:
        for _ in range(3):
            fresh = await reload_plugin(plugin, folder, registry)
            report.loaded[0] = fresh
            plugin = fresh

        stats = plugin_stats(report)
        assert stats.alive == 1, stats
        assert stats.asleep == 0, stats
    finally:
        await plugin.stop()