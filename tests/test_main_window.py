# -*- coding: utf-8 -*-
"""Главное окно и вкладка плагинов.

Три вещи, которые здесь проверяются, — не «нарисовалось ли», а не
сломается ли главное свойство окон Sinaps: открытие интерфейса не должно
выключать микрофон.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QLabel, QPushButton)

from sinaps.plugins.view import (STATUS_ASLEEP, STATUS_DISABLED, STATUS_ERROR,
                                 STATUS_LOADED, PermissionView,
                                 PluginCard, PluginView)
from sinaps.ui.main_window import SECTIONS, MainWindow
from sinaps.ui.plugins_view import PluginsView


def labels_in(widget) -> list[str]:
    """Тексты всех подписей внутри виджета."""
    return [label.text() for label in widget.findChildren(QLabel)]


def buttons_in(widget) -> list[str]:
    """Надписи всех кнопок внутри виджета."""
    return [b.text() for b in widget.findChildren(QPushButton)]


@pytest.fixture(scope="module")
def app():
    """Один `QApplication` на модуль: второй Qt не создаётся."""
    existing = QApplication.instance()
    if existing is not None:
        yield existing
        return
    application = QApplication([])
    yield application


@pytest.fixture
def window(app):
    win = MainWindow()
    yield win
    win.deleteLater()


def card(**kwargs) -> PluginCard:
    base = dict(name="weather", version="1.0.0",
                description="Погода", status=STATUS_LOADED, rss_mb=4.0,
                commands=("weather.current",))
    base.update(kwargs)
    return PluginCard(**base)


# --- окно не забирает фокус --------------------------------------------------


def test_window_does_not_steal_focus(window):
    """Открытие окна не должно выключать микрофон.

    Ровно это уже чинили в панели возможностей: окно без
    `WA_ShowWithoutActivating` перехватывало клавиатуру, и человек не мог
    сказать фразу ровно тогда, когда хотел спросить.
    """
    assert window.testAttribute(
        Qt.WidgetAttribute.WA_ShowWithoutActivating), (
        "окно активирует себя само и выключает микрофон")


def test_input_works_even_without_focus(window):
    """Строка ввода принимает текст — окном можно пользоваться.

    Запрет на автоактивацию не должен означать «в это окно нельзя
    ввести фразу». Человек сам щёлкает по строке и пишет.
    """
    window._input.setFocus()
    window._input.setText("открой блокнот")

    got: list[str] = []
    window.phrase_entered.connect(got.append)

    window._on_enter()

    assert got == ["открой блокнот"]
    assert window._input.text() == "", "строка не очистилась после отправки"


def test_empty_input_sends_nothing(window):
    """Пустая строка не отправляется.

    Иначе пробел или перевод строки превращаются в фразу для
    ассистента, и он отвечает на пустоту.
    """
    got: list[str] = []
    window.phrase_entered.connect(got.append)

    window._input.setText("   ")
    window._on_enter()

    assert got == []


# --- разделы -----------------------------------------------------------------


def test_all_sections_have_buttons(window):
    """Каждый раздел из списка имеет кнопку и страницу."""
    for key, title in SECTIONS:
        assert key in window._buttons, f"нет кнопки «{title}»"


def test_switching_sections_switches_stack(window):
    """Кнопка показывает свою страницу, а не соседнюю."""
    for index, (key, _title) in enumerate(SECTIONS):
        window.show_section(key)
        assert window._stack.currentIndex() == index, (
            f"раздел «{key}» открыл не ту страницу")
        assert window._buttons[key].isChecked(), (
            f"кнопка «{key}» не подсвечена")


def test_section_signal_emitted(window):
    """Раздел сообщает наружу — трею это нужно для своих окон."""
    got: list[str] = []
    window.section_requested.connect(got.append)

    window.show_section("history")

    assert got == ["history"]


def test_placeholder_says_it_is_not_done(window):
    """Незаконченный раздел говорит об этом, а не показывает пустоту."""
    window.show_section("settings")

    current = window._stack.currentWidget()
    texts = labels_in(current)

    assert any("1.2" in t for t in texts), (
        f"заглушка не говорит, что раздел ещё не сделан: {texts}")


# --- вкладка плагинов --------------------------------------------------------


def test_empty_view_shows_hint(app):
    """Нет плагинов — есть подсказка, куда их положить."""
    view = PluginsView()
    view.set_view(PluginView())

    texts = labels_in(view)

    assert any("manifest.yaml" in t for t in texts), texts


def test_status_shown_as_words_not_only_color(app):
    """Статус написан словом.

    Различать цвета приходится не всем, а «спит» и «активен» — это
    разница, из-за которой человек решает, грузить ли плагин заново.
    """
    view = PluginsView()
    view.set_view(PluginView(cards=[card(status=STATUS_ASLEEP)]))

    texts = labels_in(view)

    assert "спит" in texts, texts


def test_asleep_plugin_shows_dash_not_stale_memory(app):
    """У спящего плагина прочерк, а не его прошлая память.

    Показывать память закрытого процесса — значит врать: человек решит,
    что плагин занимает место, которого он уже не занимает.
    """
    assert card(status=STATUS_ASLEEP, rss_mb=4.0).memory_label == "—"
    assert card(status=STATUS_LOADED, rss_mb=4.0).memory_label == "4.0 МБ"


def test_error_plugin_shows_reason(app):
    """Плагин с ошибкой показывает причину, а не только «ошибка»."""
    view = PluginsView()
    view.set_view(PluginView(cards=[card(
        name="broken", status=STATUS_ERROR,
        error="syntax error, line 3", can_reload=False)]))

    texts = labels_in(view)

    assert any("syntax error" in t for t in texts), texts


def test_permission_shown_in_human_words(app):
    """Право показывается по-русски, а не как `platform.power`."""
    view = PluginsView()
    view.set_view(PluginView(cards=[card(permissions=(
        PermissionView("network.http", "Отправлять запросы в интернет",
                       granted=False),
    ))]))

    texts = labels_in(view)

    assert any("Отправлять запросы" in t for t in texts), texts
    assert not any("network.http" in t for t in texts), (
        f"человеку показано имя права вместо объяснения: {texts}")


def test_ungranted_permission_offers_approval(app):
    """Неодобренное право даёт кнопку одобрения."""
    view = PluginsView()
    view.set_view(PluginView(cards=[card(permissions=(
        PermissionView("state", "Хранить данные", granted=False),
    ))]))

    buttons = buttons_in(view)

    assert "Одобрить права" in buttons, buttons


def test_disabled_plugin_cannot_be_reloaded(app):
    """Выключенный плагин нельзя перезагрузить кнопкой.

    Его команда убрана из реестра намеренно. Кнопка «Перезагрузить» на
    таком плагине вернула бы то, что человек отключил, — и тихо.
    """
    view = PluginsView()
    view.set_view(PluginView(cards=[card(can_reload=False)]))

    buttons = buttons_in(view)

    assert "Перезагрузить" not in buttons, buttons


def test_reload_signal_carries_plugin_name(app):
    """Кнопка перезагрузки сообщает имя плагина."""
    view = PluginsView()
    view.set_view(PluginView(cards=[card(name="weather")]))

    got: list[str] = []
    view.reload_requested.connect(got.append)

    for button in view.findChildren(QPushButton):
        if button.text() == "Перезагрузить":
            button.click()
            break

    assert got == ["weather"]


# --- сводка ------------------------------------------------------------------


def test_summary_counts_states():
    """Сводка считает активные, спящие и с ошибкой."""
    view = PluginView(cards=[
        card(name="a", status=STATUS_LOADED),
        card(name="b", status=STATUS_ASLEEP),
        card(name="c", status=STATUS_ERROR),
    ], total=3, failed=1)

    summary = view.summary
    assert "всего 3" in summary
    assert "активных 1" in summary
    assert "спящих 1" in summary
    assert "с ошибкой 1" in summary


def test_summary_when_no_plugins():
    """Нет плагинов — это видно, а не пустая строка."""
    assert "не загружены" in PluginView().summary


def test_window_shows_summary(window):
    """Окно показывает сводку из того же снимка."""
    view = PluginView(cards=[card()], total=1)
    window.set_plugins_view(view)

    assert window._summary.text() == view.summary