# -*- coding: utf-8 -*-
"""Путь уведомления до окна: от `load_plugin` до трея.

Проверки из `tests/test_plugin_notify.py` смотрели на обработчик,
подставленный руками. Эта цепочка длиннее: загрузчик → процесс →
приложение → окно и голос. Рваться она может в любом месте, и молча:
такой обрыв выглядит как «ассистент сломался», а не как «уведомления
не доходят».
"""

from __future__ import annotations

from sinaps.plugins.protocol import CAP_NOTIFY


def test_reload_keeps_the_handler(tmp_path):
    """После перезагрузки плагин всё ещё может показать уведомление.

    Таймер переживает перезагрузку команд (пользователь нажал
    «перезагрузить»), и терять уведомления он не должен: молчащий
    таймер выглядит как сломанный.
    """
    import inspect

    from sinaps.plugins.loader import reload_plugin

    assert "notify_handler" in inspect.signature(reload_plugin).parameters, (
        "перезагрузка не умеет передать обработчик: уведомления пропадут")


def test_runtime_carries_the_handler():
    """Ассистент помнит обработчик для перезагрузок."""
    from sinaps.runtime import Runtime

    assert hasattr(Runtime, "notify_handler"), (
        "рантайм не хранит обработчик — перезагрузка его потеряет")


def test_app_shows_and_speaks():
    from PySide6.QtWidgets import QApplication

    _ = QApplication.instance() or QApplication([])
    """Трей показывает уведомление и говорит вслух.

    Обе части нужны. Только окно — человек, смотрящий в сторону,
    не заметит. Только голос — тот, кто в наушниках, не заметит.
    """
    from sinaps.core.config import SinapsConfig
    from sinaps.ui.qt_host import QtHost

    host = QtHost.__new__(QtHost)
    shown: list[tuple[str, str]] = []
    spoken: list[str] = []
    spoken_scheduled: list = []

    host._notify = lambda title, body: shown.append((title, body))
    host._runtime = None

    host._on_plugin_notify("Таймер", "Пора встать")

    assert shown == [("Таймер", "Пора встать")]

    class _Conv:
        async def speak_now(self, text):
            spoken.append(text)
            return True

    class _RT:
        conversation = _Conv()

    host._runtime = _RT()
    host._call_async = lambda factory: spoken_scheduled.append(factory)

    host._on_plugin_notify("Таймер", "Пора встать")

    assert spoken_scheduled, "ассистент не заговорил без запроса"