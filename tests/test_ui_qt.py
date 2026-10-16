"""Тесты интерфейса: трей, окна, автозапуск.

Qt здесь работает на платформе `offscreen` — она рисует в память и не
нуждается в видимом рабочем столе. Это позволяет гонять тесты в
headless-сборке, где настоящий трей всё равно недоступен.

Важно: переменная `QT_QPA_PLATFORM` выставляется до создания
`QApplication` — Qt читает её один раз, при инициализации, и сменить
платформу «на лету» нельзя.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 не установлен — этап 3 пропущен")

from PySide6.QtCore import QSize          # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from sinaps.core.session import State     # noqa: E402


# --- приложение на весь набор ----------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    """Один QApplication на модуль: второй экземпляр Qt не создаётся."""
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


# --- иконки состояний -------------------------------------------------------


def test_every_session_state_has_a_colour():
    """Каждое состояние сессии обязано иметь свой цвет.

    Без этой проверки забытое состояние молча рисуется серым «сплю»,
    и человек видит «ассистент не слушает», хотя тот слушает.
    """
    from sinaps.ui.tray import STATES

    for state in State:
        assert state.value in STATES, (
            f"состояние {state.value!r} не описано в трее — "
            "индикатор соврёт о состоянии ассистента"
        )


def test_paused_is_described():
    """Пауза — состояние интерфейса, не сессии, но индикатор её рисует."""
    from sinaps.ui.tray import STATES

    assert "paused" in STATES


def test_states_have_valid_colours():
    from sinaps.ui.tray import STATES

    for name, (colour, title, detail) in STATES.items():
        assert colour.startswith("#") and len(colour) == 7, f"{name}: цвет {colour!r}"
        assert title, f"{name}: нет подписи"
        assert detail, f"{name}: нет пояснения"


def test_paint_icon_produces_all_sizes(qapp):
    """Трей рисует иконку в 16/32/64 — все три должны быть непустыми."""
    from sinaps.ui.tray import paint_icon

    for size in (16, 32, 64):
        pixmap = paint_icon("idle", size=size).pixmap(QSize(size, size))
        assert not pixmap.isNull(), f"иконка {size}px не отрисовалась"
        assert pixmap.width() == size


def test_unknown_state_falls_back_to_idle(qapp):
    """Неизвестное состояние не должно ронять отрисовку."""
    from sinaps.ui.tray import paint_icon

    assert not paint_icon("что-то_новое", size=32).pixmap(32, 32).isNull()


def test_listening_and_thinking_have_different_colours():
    """Смысл индикатора — различать фазы, а не украшать трей."""
    from sinaps.ui.tray import STATES

    assert STATES["listening"][0] != STATES["thinking"][0]
    assert STATES["speaking"][0] != STATES["thinking"][0]


# --- трей -------------------------------------------------------------------


def test_tray_exposes_every_action(qapp):
    """В меню трея должны быть все пункты управления.

    Пропавшая кнопка выхода — это ассистент, который нельзя закрыть,
    а пропавший «Пауза» — источник неприятных срабатываний.
    """
    from sinaps.ui.tray import TrayIcon

    tray = TrayIcon()
    texts = [a.text() for a in tray._menu.actions()]   # noqa: SLF001
    for expected in ("Пауза", "Озвучка ответа", "История…",
                     "Настройки…", "Очистить историю", "Выход"):
        assert expected in texts, f"в меню трея нет пункта {expected!r}"


def test_tray_reports_status_to_menu(qapp):
    from sinaps.ui.tray import TrayIcon

    tray = TrayIcon()
    tray.bridge.status_changed.emit("thinking", "понимаю намерение")
    qapp.processEvents()

    assert tray._status_action.text() == "Думаю"      # noqa: SLF001
    assert "понимаю намерение" in tray._detail_action.text()   # noqa: SLF001
    assert "Думаю" in tray.toolTip()


def test_view_syncs_checkboxes_without_loop(qapp):
    """Программная установка галочки не должна слать сигнал обратно.

    Иначе «Пауза» в трее отражает состояние, а звучит как второе
    переключение — ассистент замирает при включении паузы и оживает
    при выключении, ровно наоборот.
    """
    from sinaps.ui.tray import TrayIcon, TrayView

    tray = TrayIcon()
    seen: list[bool] = []
    tray.bridge.pause_toggled.connect(seen.append)

    tray.bridge.view_changed.emit(TrayView(state="idle", listening=False))
    qapp.processEvents()

    assert tray._pause_action.isChecked()             # noqa: SLF001
    assert seen == [], "программная синхронизация не должна быть событием"


def test_view_pause_flips_icon_and_tooltip(qapp):
    from sinaps.ui.tray import TrayIcon, TrayView

    tray = TrayIcon()
    tray.bridge.view_changed.emit(TrayView(state="listening", listening=False))
    qapp.processEvents()

    assert "Пауза" in tray.toolTip(), "на паузе трей обязан говорить «Пауза»"


def test_unknown_view_object_is_ignored(qapp):
    """В поток обмена может прилететь что угодно — трей обязан выжить."""
    from sinaps.ui.tray import TrayIcon

    tray = TrayIcon()
    tray.bridge.view_changed.emit("не то что ожидали")
    qapp.processEvents()

    assert tray.current_view().state == "idle"


def test_view_ignored_when_tray_missing(qapp):
    """Без трея Windows сообщение просто некуда слать — это не ошибка."""
    tray = QSystemTrayIcon()
    tray.showMessage("Sinaps", "проверка")


# --- автозапуск -------------------------------------------------------------


class TestAutostart:
    """Реестр трогаем по-настоящему, но во временной записи.

    Хэш имени зависит от пути проекта, поэтому `tmp_path` даёт запись,
    которой нет у реального ассистента — и её можно удалить в финале,
    не тронув настройки пользователя.
    """

    @pytest.fixture(autouse=True)
    def _cleanup(self, tmp_path):
        from sinaps.platform import autostart

        yield
        autostart.disable(tmp_path)

    def test_disabled_by_default(self, tmp_path):
        from sinaps.platform import autostart

        assert autostart.is_enabled(tmp_path) is False

    def test_enable_disable_roundtrip(self, tmp_path):
        from sinaps.platform import autostart

        assert autostart.enable(tmp_path) is True
        assert autostart.is_enabled(tmp_path) is True
        assert autostart.disable(tmp_path) is True
        assert autostart.is_enabled(tmp_path) is False

    def test_disabling_absent_entry_is_success(self, tmp_path):
        """Выключить то, чего нет — не ошибка: цель уже достигнута."""
        from sinaps.platform import autostart

        assert autostart.disable(tmp_path) is True

    def test_set_enabled(self, tmp_path):
        from sinaps.platform import autostart

        assert autostart.set_enabled(True, tmp_path) is True
        assert autostart.is_enabled(tmp_path) is True
        assert autostart.set_enabled(False, tmp_path) is True
        assert autostart.is_enabled(tmp_path) is False

    def test_command_points_at_interpreter_without_console(self):
        """Без `pythonw` в автозапуске пользователь получит чёрное окно."""
        from sinaps.platform import autostart

        command = autostart._command()      # noqa: SLF001
        assert "-m sinaps" in command
        assert "pythonw" in command.lower(), f"команда {command!r} откроет консоль"
        assert '"' in command, "путь с пробелами должен быть в кавычках"

    def test_command_starts_tray_mode(self):
        """Без `--tray` автозапуск поднимает консольный режим.

        Под pythonw окна консоли нет, интерактивный ввод недоступен —
        ассистент висит невидимым и не отвечает. При загрузке Windows
        это выглядит как «программа молча сломалась».
        """
        from sinaps.platform import autostart

        assert "--tray" in autostart._command()      # noqa: SLF001

    def test_entry_name_is_stable_and_unique(self, tmp_path):
        """Две копии проекта не должны затирать записи друг друга."""
        from sinaps.platform import autostart

        first = autostart._value_name(tmp_path)    # noqa: SLF001
        second = autostart._value_name(tmp_path)   # noqa: SLF001
        other = autostart._value_name(tmp_path / "копия")   # noqa: SLF001

        assert first == second, "имя записи должно быть устойчивым"
        assert first != other, "у другой копии должно быть другое имя"
        assert first.startswith("Sinaps-")


# --- хост -------------------------------------------------------------------


def test_qt_available_on_windows():
    """Проверка не должна ронять Qt: она вызывается до QApplication."""
    from sinaps.ui.qt_host import qt_available

    ok, reason = qt_available()
    assert ok is (sys.platform == "win32")
    if not ok:
        assert reason, "недоступность должна объясняться словами"


def test_host_does_not_write_history_itself():
    """Историю пишет конвейер, и только он.

    Если хост подпишется на `transcript_final` и `response_ready` ещё и
    сам, каждый обмен ляжет в базу дважды, а вместе с ним — служебные
    реплики («секунду, я ещё отвечаю»), которые разговором не являются.
    """
    from pathlib import Path

    from sinaps.ui import qt_host

    source = Path(qt_host.__file__).read_text(encoding="utf-8")
    subscribe_block = source.split("def _subscribe", 1)[1].split("\n    def ", 1)[0]

    assert "add_dialogue" not in subscribe_block, (
        "хост не должен писать историю — это делает ConversationPipeline"
    )
    assert "add_turn" not in subscribe_block
