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


def test_all_sections_switchable_in_a_row(window):
    """Перебор всех разделов подряд не падает.

    Именно этот порядок проверяет то, что проверять было нужно: раньше
    переход на «Историю» или «Настройки» ронял процесс с переполнением
    стека, потому что обработчик раздела звал `show_section` снова.
    Тест с безобидным подписчиком этого не видел: рекурсия жила в
    связке с настоящим обработчиком, а он в тесте не участвовал.
    """
    for _pass in range(3):
        for key, _title in SECTIONS:
            window.show_section(key)


def test_repeated_same_section_is_safe(window):
    """Повторный выбор того же раздела тоже безопасен."""
    window.show_section("history")
    window.show_section("history")
    window.show_section("history")

    assert window._stack.currentIndex() == 1


def test_switching_back_and_forth_between_two_sections(window):
    """Быстрое переключение туда-обратно не копит ничего.

    Рекурсия обнаруживается не сразу: она растёт по одному вызову за
    клик. Один переход может пройти незаметно, и падение случится на
    третьем.
    """
    for _ in range(50):
        window.show_section("settings")
        window.show_section("history")
        window.show_section("plugins")


def test_settings_section_is_real_not_placeholder(window):
    """Раздел «Настройки» рабочий, а не заглушка.

    Раньше здесь стояла надпись «появится в Beta 1.2». Теперь раздел
    настоящий, и тест проверяет именно это: заглушка вернулась бы так
    же незаметно, как её убрали.
    """
    from sinaps.ui.settings_view import SettingsView

    window.show_section("settings")
    page = window._stack.currentWidget()

    assert isinstance(page, SettingsView)
    assert page._rows, "в разделе настроек нет ни одной строки"


def test_history_section_is_real(window):
    """Раздел «История» рабочий."""
    from sinaps.ui.history_view import HistoryView

    window.show_section("history")

    assert isinstance(window._stack.currentWidget(), HistoryView)


def test_no_placeholder_text_anywhere(window):
    """Ни в одном разделе не осталось слов про «Beta 1.2».

    Поиск по всем подписям, а не по одному разделу: заглушка могла бы
    остаться в том, который никто не открывает.
    """
    window.show_section("history")
    window.set_history([], None, "История пуста")
    window.show_section("settings")
    window.show_section("help")

    for key, _title in SECTIONS:
        window.show_section(key)
        texts = labels_in(window._stack.currentWidget())
        for text in texts:
            assert "Beta 1.2" not in text, (
                f"в разделе «{key}» осталась заглушка: {text!r}")
            assert "появится" not in text, (
                f"в разделе «{key}» обещание будущего: {text!r}")


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