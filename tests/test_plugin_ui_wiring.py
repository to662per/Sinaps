# -*- coding: utf-8 -*-
"""Связка главного окна с ассистентом: что происходит по нажатию.

Здесь важно не «нарисовалось ли», а что кнопки делают с настоящим
состоянием. Ошибка, которая тут же повторится в Beta 1.2, — обработать
нажатие и забыть применить.

Класс `QtHost` проверяется без запуска Qt-приложения: подменяются
только те части, что ходят в ассистент.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from sinaps.plugins.approvals import grant
from sinaps.plugins.loader import load_all
from sinaps.plugins.view import STATUS_DISABLED, STATUS_LOADED
from sinaps.skills.registry import SkillRegistry
from sinaps.ui.qt_host import QtHost

log = logging.getLogger(__name__)


PLUGIN = '''\
from sinaps.plugins.api import Response, Skill

async def handle(ctx, slots):
    return Response(speech="услышал")

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
description: Плагин для проверки
entry: plugin:register
permissions:
{permissions}
"""


def make_plugin(folder, name="weather", permissions="  - state"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(
        MANIFEST.format(name=name, permissions=permissions),
        encoding="utf-8")
    (folder / "plugin.py").write_text(PLUGIN, encoding="utf-8")
    return folder


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    if existing is not None:
        yield existing
        return
    yield QApplication([])


@pytest.fixture(autouse=True)
def isolated_approvals(tmp_path, monkeypatch):
    from sinaps.plugins import approvals

    monkeypatch.setattr(approvals, "path", lambda: tmp_path / "plugins.json")


class FakeRuntime:
    """Ассистент целиком в заглушках — Qt тут ни при чём."""

    def __init__(self, registry: SkillRegistry, report) -> None:
        self.registry = registry
        self.plugins = report
        self.disabled_plugins: set[str] = set()
        self.session = types_session()
        self.listener = None
        self.conversation = None
        self.reloaded: list[str] = []

    def plugin_view(self):
        from sinaps.plugins.view import build_plugin_view

        return build_plugin_view(
            self.plugins, disabled=frozenset(self.disabled_plugins))

    def plugin_names(self):
        return tuple(p.name for p in self.plugins.loaded)

    async def reload_plugin(self, name: str) -> bool:
        from sinaps.plugins.loader import reload_plugin, replace_plugin

        self.reloaded.append(name)
        plugin = next((p for p in self.plugins.loaded if p.name == name), None)
        if plugin is None:
            return False
        fresh = await reload_plugin(
            plugin, plugin.process.folder, self.registry)
        replace_plugin(self.plugins, plugin, fresh)
        return True


def types_session():
    """Состояние сессии — как в настоящем, но без цикла событий."""
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(value="idle"))


@pytest.fixture
def host(app):
    """`QtHost` без трея и без окна запуска."""
    host = QtHost.__new__(QtHost)
    host.main_window = None
    host.history_window = None
    host.settings_window = None
    host.panel = None
    host._runtime = None
    host.loop = None
    host.tray = None
    host._paused = False
    host._speak_enabled = True
    host.notifications = []
    return host


@pytest.fixture
async def inline_loop(host):
    """`_call_async` выполняет действие сразу, без отдельного потока.

    Боевой `QtHost` отправляет корутину в чужой event loop, потому что
    ассистент живёт в asyncio-потоке. В тесте потока нет, поэтому
    задачи нужно собрать, иначе проверка шла бы до их выполнения и
    видела бы намерение, а не результат.

    Сбор идёт через фабрику задач цикла: `_call_async` создаёт их
    внутри `asyncio.ensure_future`, и перехватить их иначе нельзя.
    """
    loop = asyncio.get_running_loop()
    handle = _LoopHandle()
    original_factory = loop.get_task_factory()

    def tracking_factory(loop_ref, coro, **kwargs):  # noqa: ANN001
        task = asyncio.tasks.Task(coro, loop=loop_ref, **kwargs)
        handle.pending.append(task)
        return task

    loop.set_task_factory(tracking_factory)
    handle.host = host
    host.loop = _InlineLoop(handle)
    try:
        yield handle
    finally:
        loop.set_task_factory(original_factory)
        host.loop = None


class _InlineLoop:
    """Заглушка event loop: выполняет действие сразу, в текущем потоке."""

    def __init__(self, handle: "_LoopHandle") -> None:
        self._handle = handle

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, func, *args) -> None:
        func(*args)


class _LoopHandle:
    """Хост с настроенным inline-loop и список незавершённых задач."""

    def __init__(self) -> None:
        self.host = None
        self.pending: list = []

    async def drain(self) -> None:
        """Дождаться всех задач, запущенных через `_call_async`."""
        while self.pending:
            task = self.pending.pop(0)
            try:
                await task
            except Exception:                 # noqa: BLE001
                # Ошибку действия логируем, но не роняем: интересует она
                # конкретный тест, а не этот помощник. Роняя здесь, мы
                # подменяем причину: тест упал бы на `drain`, и падение
                # указывало бы не туда.
                log.debug("действие из окна упало", exc_info=True)


# --- одобрение прав ----------------------------------------------------------


async def test_approve_writes_to_approvals(tmp_path, inline_loop):
    """Кнопка «Одобрить права» пишет в approvals, а не мимо него.

    Файл один: если писать в обход, права из окна и права из голоса
    разошлись бы, и человек не смог бы понять, что на что он одобрил.
    """
    host = inline_loop.host
    from sinaps.plugins import approvals

    make_plugin(tmp_path / "a", name="a")
    registry = SkillRegistry()
    report = await load_all(tmp_path, registry)
    runtime = FakeRuntime(registry, report)
    host._runtime = runtime

    try:
        host._approve_plugin_from_ui("a")
        await inline_loop.drain()

        assert approvals.granted_for("a", ["state"]) == ("state",), (
            "одобрение не записалось")
        assert runtime.reloaded == ["a"], (
            "плагин не перезагружен: право действует только после подъёма")
    finally:
        await report.stop_all()


async def test_approve_for_unknown_plugin_does_nothing(
        tmp_path, inline_loop):
    """Одобрение плагина, которого нет, ничего не пишет."""
    host = inline_loop.host
    from sinaps.plugins import approvals

    registry = SkillRegistry()
    report = await load_all(tmp_path, registry)
    host._runtime = FakeRuntime(registry, report)
    try:
        host._approve_plugin_from_ui("нет_такого")

        assert approvals.approved() == {}
    finally:
        await report.stop_all()


async def test_revoke_removes_approval(tmp_path, inline_loop):
    """«Отозвать» убирает одобрение из файла."""
    host = inline_loop.host
    from sinaps.plugins import approvals

    make_plugin(tmp_path / "a", name="a")
    grant("a", ["state"])
    registry = SkillRegistry()
    report = await load_all(tmp_path, registry)
    host._runtime = FakeRuntime(registry, report)

    try:
        host._revoke_plugin_from_ui("a")

        assert approvals.granted_for("a", ["state"]) == ()
    finally:
        await report.stop_all()


# --- выключение --------------------------------------------------------------


async def test_disable_removes_commands(tmp_path, inline_loop):
    """Выключение убирает команды из реестра.

    Иначе окно покажет «выключен», а команда продолжит отвечать — и
    человек решит, что отключение не сработало, и напишет об ошибке.
    """
    host = inline_loop.host
    make_plugin(tmp_path / "a", name="a")
    registry = SkillRegistry()
    report = await load_all(tmp_path, registry)
    runtime = FakeRuntime(registry, report)
    host._runtime = runtime

    try:
        assert "a.get.now" in registry.spec_names()

        host._toggle_plugin_from_ui("a", enable=False)
        await inline_loop.drain()

        assert "a.get.now" not in registry.spec_names()
        assert "a" in runtime.disabled_plugins
        assert runtime.plugin_view().cards[0].status == STATUS_DISABLED
    finally:
        await report.stop_all()


async def test_enable_brings_commands_back(tmp_path, inline_loop):
    """Включение возвращает команды."""
    host = inline_loop.host
    make_plugin(tmp_path / "a", name="a")
    registry = SkillRegistry()
    report = await load_all(tmp_path, registry)
    runtime = FakeRuntime(registry, report)
    host._runtime = runtime

    try:
        host._toggle_plugin_from_ui("a", enable=False)
        assert "a.get.now" not in registry.spec_names()

        host._toggle_plugin_from_ui("a", enable=True)
        await inline_loop.drain()

        assert "a" not in runtime.disabled_plugins
        assert "a.get.now" in registry.spec_names()
    finally:
        await report.stop_all()


# --- перезагрузка ------------------------------------------------------------


async def test_reload_goes_through_runtime(tmp_path, inline_loop):
    """Кнопка перезагрузки зовёт рантайм, а не процесс напрямую.

    Рантайм знает про отчёт и про шину; обработчик окна знает про кнопки.
    Смешивать эти две вещи — значит получить два места, где плагин
    перезапускается, и рано или поздно они разойдутся.
    """
    host = inline_loop.host
    make_plugin(tmp_path / "a", name="a")
    registry = SkillRegistry()
    report = await load_all(tmp_path, registry)
    runtime = FakeRuntime(registry, report)
    host._runtime = runtime

    try:
        host._reload_plugin_from_ui("a")
        await inline_loop.drain()

        assert runtime.reloaded == ["a"]
    finally:
        await report.stop_all()


async def test_reload_without_runtime_is_safe(inline_loop):
    """Перезагрузка без ассистента не падает."""
    host = inline_loop.host
    host._runtime = None

    host._reload_plugin_from_ui("a")   # не должно бросить


# --- снимок для окна ---------------------------------------------------------


async def test_refresh_shows_plugins(tmp_path, host):
    """Перед показом окно получает свежий снимок плагинов."""
    make_plugin(tmp_path / "a", name="a")
    registry = SkillRegistry()
    report = await load_all(tmp_path, registry)
    host._runtime = FakeRuntime(registry, report)

    try:
        from sinaps.ui.main_window import MainWindow

        host.main_window = MainWindow()
        host._refresh_main_window()

        assert host.main_window._summary.text()
        assert host.main_window._status.text() == "Спит"
    finally:
        await report.stop_all()


async def test_refresh_without_runtime_says_so(host):
    """Без ассистента окно говорит об этом, а не молчит."""
    from sinaps.ui.main_window import MainWindow

    host._runtime = None
    host.main_window = MainWindow()

    host._refresh_main_window()

    assert "не запущен" in host.main_window._summary.text()


async def test_plugin_status_reflects_reap(tmp_path, host):
    """После уборки карточка показывает «спит».

    Статус должен следовать за процессом, а не за фактом присутствия
    плагина в отчёте: иначе окно врало бы, что плагин отвечает.
    """
    from sinaps.plugins.loader import reap_idle

    make_plugin(tmp_path / "a", name="a")
    registry = SkillRegistry()
    report = await load_all(tmp_path, registry)
    host._runtime = FakeRuntime(registry, report)

    try:
        assert host._runtime.plugin_view().cards[0].status == STATUS_LOADED

        await reap_idle(report, 0)

        assert host._runtime.plugin_view().cards[0].status_title == "спит"
    finally:
        await report.stop_all()