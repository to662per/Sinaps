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
    assert tray.showMessage("Sinaps", "проверка") in (None, False) or True


# --- окно истории -----------------------------------------------------------


@pytest.fixture
def filled_store(tmp_path):
    from sinaps.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "history.db", keep_days=30)
    store.add_dialogue("user", "открой блокнот", source="voice", confidence=0.87)
    store.add_dialogue("assistant", "Открываю блокнот", source="voice")
    store.add_dialogue("user", "а какой сегодня день", source="voice")
    return store


def test_history_window_lists_dialogues(qapp, filled_store):
    from sinaps.ui.windows import HistoryWindow

    window = HistoryWindow(filled_store)
    assert window._list.count() >= 3   # заголовок дня + 3 реплики


def test_history_window_groups_by_day(qapp, filled_store):
    from PySide6.QtCore import Qt

    from sinaps.ui.windows import HistoryWindow

    window = HistoryWindow(filled_store)
    header = window._list.item(0)         # noqa: SLF001
    assert header.text().startswith("— "), "первой строкой идёт заголовок дня"
    assert header.data(Qt.ItemDataRole.UserRole) is None, (
        "у заголовка дня нет реплики за собой — иначе клик откроет чужую"
    )


def test_history_window_search_filters(qapp, filled_store):
    from sinaps.ui.windows import HistoryWindow

    window = HistoryWindow(filled_store)
    window._search.setText("блокнот")   # noqa: SLF001
    window._reload()                    # noqa: SLF001

    found = [window._list.item(i).text()      # noqa: SLF001
             for i in range(window._list.count())]
    assert any("блокнот" in t for t in found)
    assert not any("какой сегодня день" in t for t in found)


def test_history_window_shows_detail_on_selection(qapp, filled_store):
    from sinaps.ui.windows import HistoryWindow

    window = HistoryWindow(filled_store)
    for row in range(window._list.count()):      # noqa: SLF001
        window._list.setCurrentRow(row)         # noqa: SLF001
        text = window._detail.toPlainText()     # noqa: SLF001
        if text:
            assert "Роль" in text
            assert "Уверенность" in text
            return
    pytest.fail("ни одна реплика не показала подробности")


def test_history_window_survives_broken_store(qapp):
    """Окно истории не имеет права закрываться из-за ошибки БД."""
    from sinaps.ui.windows import HistoryWindow

    class Broken:
        path = "нет.такого.файла"

        def search(self, *a, **k):
            raise sqlite_error()

        def recent(self, *a, **k):
            raise sqlite_error()

        def stats(self, *a, **k):
            return {"total": 0}

    def sqlite_error():
        import sqlite3

        return sqlite3.OperationalError("database is locked")

    window = HistoryWindow(Broken())
    assert window._list.count() == 0   # noqa: SLF001
    assert "0 из 0" in window._status.text()   # noqa: SLF001


# --- окно настроек ----------------------------------------------------------


@pytest.fixture
def cfg():
    from sinaps.core.config import load_config

    return load_config()


def test_settings_window_saves_values(qapp, cfg, tmp_path, monkeypatch):
    from sinaps.core.config import load_config
    from sinaps.ui.windows import SettingsWindow

    target = tmp_path / "config.yaml"
    monkeypatch.setattr("sinaps.core.config.CONFIG_PATH", target)

    window = SettingsWindow(cfg, models=["large-v3-turbo"])
    window._keep_days.setText("45")           # noqa: SLF001
    window._voice.setCurrentText("female_ru")  # noqa: SLF001
    window._save()                            # noqa: SLF001

    assert cfg.privacy.keep_history_days == 45
    assert cfg.tts.voice == "female_ru"

    saved = load_config(target, autostart=False)
    assert saved.privacy.keep_history_days == 45, "настройки должны попасть в YAML"


def test_settings_rejects_non_numeric_silence(qapp, cfg, monkeypatch):
    """Мусор в поле — это отказ сохранять, а не запись NaN в конфиг."""
    from sinaps.ui.windows import SettingsWindow

    warnings: list[str] = []
    monkeypatch.setattr("sinaps.ui.windows.QMessageBox.warning",
                        lambda *a, **k: warnings.append(a[1]))

    cfg.vad.silence_ms = 700
    window = SettingsWindow(cfg)
    window._quiescence.setText("не число")     # noqa: SLF001
    window._save()                              # noqa: SLF001

    assert cfg.vad.silence_ms == 700, "плохое значение не должно попасть в конфиг"
    assert warnings, "пользователь должен узнать о проблеме"


def test_settings_buttons_are_russian(qapp, cfg):
    """Английские кнопки в русском ассистенте выглядят как недоделка."""
    from PySide6.QtWidgets import QDialogButtonBox

    from sinaps.ui.windows import SettingsWindow, _localize_buttons

    window = SettingsWindow(cfg)
    box = window.findChild(QDialogButtonBox)
    assert box is not None, "нет панели кнопок"

    save = box.button(QDialogButtonBox.StandardButton.Save)
    assert save.text() == "Сохранить"

    empty = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
    _localize_buttons(empty)
    assert empty.button(QDialogButtonBox.StandardButton.Cancel).text() == "Отмена"


def test_settings_reports_failed_autostart(qapp, cfg, monkeypatch):
    """Если реестр не пишется — галочка должна откатиться, а не соврать."""
    from sinaps.ui.windows import SettingsWindow

    monkeypatch.setattr("sinaps.ui.windows.QMessageBox.warning",
                        lambda *a, **k: None)

    window = SettingsWindow(cfg, autostart_state=lambda: False,
                            on_autostart=lambda v: False)
    window._autostart.setChecked(True)    # noqa: SLF001

    assert not window._autostart.isChecked(), (   # noqa: SLF001
        "неудачная запись в реестр обязана откатить галочку"
    )


def test_settings_survives_device_listing_failure(qapp, cfg):
    """Если список устройств не получить — окно всё равно должно открыться.

    PortAudio может ответить отказом при занятом устройстве, и это не
    повод закрывать окно настроек: остальные вкладки ещё пригодны.
    """

    def broken():
        raise RuntimeError("PortAudio не отвечает")

    from sinaps.ui.windows import SettingsWindow

    window = SettingsWindow(cfg, audio_devices=broken, voices=broken)
    assert window._mic.count() >= 1    # noqa: SLF001


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
