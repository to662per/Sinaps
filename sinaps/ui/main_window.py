# -*- coding: utf-8 -*-
"""Главное окно Sinaps: слева разделы, справа содержимое.

Три требования, каждое из которых уже было оплачено однажды:

1. **Окно не забирает фокус при открытии.** Иначе открытие справки
   выключало бы микрофон ровно в тот момент, когда человек пытается
   что-то сказать. Поэтому `WA_ShowWithoutActivating`; клавиатура при
   этом работает — окно можно и нужно активировать щелчком, когда
   человек сам захотел в него писать.

2. **Строка ввода — тот же путь, что голос.** Никаких отдельных окон
   для набора текста: `QApplication.sendPostedEvents` в Qt-поток, дальше
   обычный конвейер разговора. Иначе один и тот же текст пошёл бы двумя
   дорогами и разошёлся бы.

3. **Состояние берётся из источника, а не из своей копии.** Плагины
   показываются так же, как пишутся в журнал: тот же `plugin_stats()`.
   Счётчик в интерфейсе, посчитанный отдельно, со временем разошёлся бы
   с журналом, и расхождение в метриках не заметил бы никто.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

from .plugins_view import PluginsView
from .theme import (CARD, DANGER, EDGE, MUTED, TEXT, button, color, font,
                    label, rgba, shell_style)

log = logging.getLogger(__name__)

#: Разделы слева. Ключ совпадает с именем виджета в стеке, чтобы не
#: держать параллельный список индексов, который разъедется при вставке.
SECTIONS = (
    ("plugins", "Плагины"),
    ("history", "История"),
    ("settings", "Настройки"),
    ("help", "Справка"),
)


class MainWindow(QWidget):
    """Единая точка входа в интерфейс."""

    #: Человек написал фразу в строке ввода.
    phrase_entered = Signal(str)
    #: Нажали «Слушать» — активация без горячей клавиши.
    listen_requested = Signal()
    #: Запрос к плагинам: имя и что с ним сделать.
    reload_requested = Signal(str)
    approve_requested = Signal(str)
    revoke_requested = Signal(str)
    toggle_plugin_requested = Signal(str, bool)
    #: Открыть раздел: ключ из `SECTIONS`.
    section_requested = Signal(str)
    #: Закрыть окно.
    quit_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle("Sinaps")
        self.resize(1000, 680)

        # Окно не активируется само: микрофон должен продолжать работать.
        self.setAttribute(
            Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._buttons: dict[str, QPushButton] = {}
        self._build()

    # --- построение -------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        shell = QFrame(self)
        shell.setObjectName("main_shell")
        shell.setStyleSheet(shell_style("main_shell"))
        outer.addWidget(shell)

        root = QHBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_content(), 1)

    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setStyleSheet(f"""
            #sidebar {{
                background: {rgba(CARD, 255)};
                border-top-left-radius: 12px;
                border-bottom-left-radius: 12px;
                border-right: 1px solid {rgba(EDGE, 180)};
            }}
        """)
        side.setFixedWidth(190)

        column = QVBoxLayout(side)
        column.setContentsMargins(12, 16, 12, 16)
        column.setSpacing(6)

        title = QLabel("Sinaps")
        title.setFont(font(14, bold=True))
        title.setStyleSheet(label(size=14, bold=True))
        column.addWidget(title)

        self._status = QLabel("Спит")
        self._status.setFont(font(9))
        self._status.setStyleSheet(label(size=9, tone=MUTED))
        self._status.setWordWrap(True)
        column.addWidget(self._status)

        column.addSpacing(14)

        for key, title_text in SECTIONS:
            section_button = QPushButton(title_text)
            section_button.setCheckable(True)
            section_button.setCursor(Qt.CursorShape.PointingHandCursor)
            section_button.clicked.connect(
                lambda _checked=False, k=key: self.show_section(k))
            column.addWidget(section_button)
            self._buttons[key] = section_button

        column.addStretch(1)

        quit_button = QPushButton("Выход")
        quit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_button.setStyleSheet(button(
            "rgba(0, 0, 0, 0)",
            f"rgba({DANGER.red()}, {DANGER.green()}, {DANGER.blue()}, 160)"))
        quit_button.clicked.connect(self.quit_requested.emit)
        column.addWidget(quit_button)

        return side

    def _build_content(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("content")

        column = QVBoxLayout(panel)
        column.setContentsMargins(18, 16, 18, 16)
        column.setSpacing(12)

        column.addWidget(self._build_toolbar())

        self._stack = QStackedWidget()
        column.addWidget(self._stack, 1)

        self._plugins = PluginsView()
        self._stack.addWidget(self._plugins)

        for key, _title in SECTIONS:
            if key == "plugins":
                continue
            self._stack.addWidget(self._build_placeholder(key))

        self._plugins.reload_requested.connect(self.reload_requested)
        self._plugins.approve_requested.connect(self.approve_requested)
        self._plugins.revoke_requested.connect(self.revoke_requested)
        self._plugins.toggle_requested.connect(
            self.toggle_plugin_requested)

        column.addWidget(self._build_input())

        return panel

    def _build_toolbar(self) -> QWidget:
        row = QHBoxLayout()

        self._summary = QLabel("")
        self._summary.setFont(font(9))
        self._summary.setStyleSheet(label(size=9, tone=MUTED))
        row.addWidget(self._summary)

        row.addStretch(1)

        listen = QPushButton("Слушать")
        listen.setCursor(Qt.CursorShape.PointingHandCursor)
        listen.setToolTip("Открыть окно прослушивания без горячей клавиши")
        listen.setStyleSheet(button(
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 170)",
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 255)"))
        listen.clicked.connect(self.listen_requested.emit)
        row.addWidget(listen)

        return self._wrap(row)

    def _build_input(self) -> QWidget:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._input = QLineEdit()
        self._input.setPlaceholderText(
            "Напишите фразу или скажите её голосом…")
        self._input.setFont(font(10))
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {rgba(CARD, 255)};
                border: 1px solid {rgba(EDGE, 200)};
                border-radius: 8px;
                padding: 8px 12px;
                color: {color(TEXT)};
                font-size: 10pt;
            }}
            QLineEdit:focus {{
                border-color: {rgba(EDGE, 255)};
            }}
        """)
        self._input.returnPressed.connect(self._on_enter)
        row.addWidget(self._input, 1)

        send = QPushButton("Сказать")
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(button(
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 190)",
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 255)"))
        send.clicked.connect(self._on_enter)
        row.addWidget(send)

        return self._wrap(row)

    def _build_placeholder(self, key: str) -> QWidget:
        """Заглушка раздела, который придёт в Beta 1.2.

        Заглушка, а не пустота: раздел есть в списке, но не сделан — и
        человек видит это сразу, а не после нажатия в пустоту.
        """
        box = QFrame()
        box.setObjectName(f"placeholder_{key}")
        box.setStyleSheet(shell_style(f"placeholder_{key}"))

        column = QVBoxLayout(box)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.setSpacing(8)

        title = QLabel(dict(SECTIONS)[key])
        title.setFont(font(12, bold=True))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(label(size=12, bold=True))
        column.addWidget(title)

        hint = QLabel("Этот раздел появится в Beta 1.2.")
        hint.setFont(font(9))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(label(size=9, tone=MUTED))
        column.addWidget(hint)

        return box

    @staticmethod
    def _wrap(layout) -> QWidget:
        box = QWidget()
        box.setLayout(layout)
        return box

    # --- состояние --------------------------------------------------------

    def show_section(self, key: str) -> None:
        """Показать раздел.

        Кнопка подсвечивается по `SECTIONS`, а не по индексу: при вставке
        нового раздела в середину списка индексы разъедутся, а поиск по
        ключу — нет.
        """
        for index, (name, _title) in enumerate(SECTIONS):
            self._buttons[name].setChecked(name == key)
            if name == key:
                self._stack.setCurrentIndex(index)
        self.section_requested.emit(key)

    def set_status(self, text: str) -> None:
        """Подпись состояния в боковом меню."""
        self._status.setText(text)

    def set_plugins_view(self, view) -> None:
        """Показать снимок плагинов."""
        self._plugins.set_view(view)
        self._summary.setText(view.summary)

    def set_plugin_summary(self, text: str) -> None:
        self._summary.setText(text)

    def set_input_enabled(self, enabled: bool) -> None:
        self._input.setEnabled(enabled)

    def _on_enter(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.phrase_entered.emit(text)


__all__ = ["MainWindow", "SECTIONS"]