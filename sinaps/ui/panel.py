"""Панель возможностей: окно поверх всего.

Окно не забирает фокус — и это не украшение, а условие работоспособности
----------------------------------------------------------------------
Спросили «что ты умеешь» — ассистент отвечает голосом. Если панель
станет обычным окном и заберёт фокус, произнесённый ответ не дойдёт до
микрофона: приложение перестаёт слушать в момент, когда человек
пытается что-то сказать. Звук при этом продолжает идти в динамики,
и кажется, что всё работает, — а не работает ничего.

Поэтому окно создаётся с флагами без фокуса и показывается без
активации. Мышь работает, голос продолжает работать, клавиатура
до панели не доходит — поэтому `Esc` здесь не слушается, зато есть
заметная кнопка закрытия и автозакрытие.

Таймер бездействия
------------------
Панель закрывается сама через 25 секунд, если человек с ней не
взаимодействует. Любое действие — движение мыши, клик, смена вкладки,
нажатие чипа — перезапускает отсчёт. Пока человек читает и думает,
таймер не идёт: закрывать окно под рукой у того, кто его открыл,
значит заставить его открывать заново.

Данные приходят готовыми из `panel_data`: виджет ничего не решает,
только рисует и отдаёт выбранную фразу.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QKeySequence, QShortcut)
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from .panel_data import IDLE_CLOSE_S, Card, PanelData

log = logging.getLogger(__name__)

#: Карточек в ряду. Фиксированное число, а не «сколько поместилось»:
#: иначе в разделе с двумя командами они растягиваются во всю ширину,
#: а в разделе с семью — теряют место. Сетка должна выглядеть одинаково.
COLUMNS = 3

#: Высота карточки. Единая для всех: описание бывает длинным и
#: переносится, и при разной высоте ряд выглядит рваным.
CARD_HEIGHT = 132
GRID_SPACING = 10

#: Палитра. Тёмная, чтобы не резала глаза вечером и читалась поверх
#: любого окна: на светлом фоне тёмная подложка, на тёмном — подложка
#: с обводкой.
_BACKDROP = QColor(18, 20, 26, 244)
_CARD = QColor(32, 36, 45, 255)
_CARD_HOVER = QColor(44, 50, 62, 255)
_EDGE = QColor(78, 86, 102, 255)
_TEXT = QColor(232, 235, 240)
_MUTED = QColor(154, 164, 178)
_ACCENT = QColor(61, 139, 253)
_DANGER = QColor(229, 83, 75)


class Panel(QWidget):
    """Окно со списком возможностей ассистента."""

    #: Человек нажал «Попробовать» или чип. Фраза уходит в ассистента.
    phrase_chosen = Signal(str)

    #: Панель закрылась сама по бездействию.
    idle_closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle("Sinaps — что я умею")
        self._data = PanelData()

        # Порядок важен: `WindowDoesNotAcceptFocus` запрещает окну
        # получать клавиатурный ввод, поэтому Esc мы не ловим вовсе,
        # а закрытие даём кнопкой. Это осознанная плата за рабочий микрофон.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)

        self._build()
        self._install_shortcut()

        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(int(IDLE_CLOSE_S * 1000))
        self._idle.timeout.connect(self._on_idle)

        # Высоты постоянных частей нужны для подгонки окна под карточки.
        # Измеряем после появления: до этого Qt не знает размеров.
        self._header_height = 120
        self._chips_height = 64
        self._vertical_padding = 56
        #: Сколько карточек сейчас в сетке — для подгонки высоты при
        #: первом показе, когда `show_group` ещё не вызывался.
        self._current_count = 0

        self.resize(900, 560)

    # --- построение -------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)

        shell = QFrame(self)
        shell.setObjectName("panel_shell")
        shell.setStyleSheet(f"""
            #panel_shell {{
                background: rgba(18, 20, 26, 246);
                border: 1px solid rgba(78, 86, 102, 220);
                border-radius: 14px;
            }}
        """)
        outer.addWidget(shell)

        layout = QVBoxLayout(shell)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        layout.addWidget(self._build_header())
        self._tabs = self._build_tabs()
        layout.addWidget(self._tabs)
        layout.addWidget(self._build_body(), 1)
        layout.addWidget(self._build_chips())

    def _build_header(self) -> QWidget:
        row = QHBoxLayout()

        title = QLabel("Что я умею")
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color: rgb({_TEXT.red()}, {_TEXT.green()}, {_TEXT.blue()});")
        row.addWidget(title)

        self._summary = QLabel("")
        self._summary.setFont(QFont("Segoe UI", 9))
        self._summary.setStyleSheet(
            f"color: rgb({_MUTED.red()}, {_MUTED.green()}, {_MUTED.blue()});")
        row.addWidget(self._summary)
        row.addStretch(1)

        hint = QLabel("нажмите «Попробовать», чтобы услышать")
        hint.setFont(QFont("Segoe UI", 9))
        hint.setStyleSheet(
            f"color: rgb({_MUTED.red()}, {_MUTED.green()}, {_MUTED.blue()});")
        row.addWidget(hint)

        close = QPushButton("✕")
        close.setFixedSize(28, 28)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(self._button_style(
            bg="rgba(60,66,80,255)", hover="rgba(229,83,75,220)", fg="white"))
        close.clicked.connect(self.close)
        row.addWidget(close)

        return self._wrap(row)

    def _build_tabs(self) -> QWidget:
        row = QHBoxLayout()
        self._tab_buttons: list[QPushButton] = []
        for index, (key, title, count) in enumerate(self._data.groups):
            button = QPushButton(f"{title}  ·  {count}")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(self._tab_style())
            button.clicked.connect(lambda _c, k=key: self.show_group(k))
            row.addWidget(button)
            self._tab_buttons.append(button)
        row.addStretch(1)
        return self._wrap(row)

    def _build_body(self) -> QWidget:
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("background: transparent;")
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(10)
        self._scroll.setWidget(self._grid_host)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
        return self._scroll

    def _build_chips(self) -> QWidget:
        row = QHBoxLayout()
        label = QLabel("Попробуйте:")
        label.setFont(QFont("Segoe UI", 9))
        label.setStyleSheet(
            f"color: rgb({_MUTED.red()}, {_MUTED.green()}, {_MUTED.blue()});")
        row.addWidget(label)

        self._chip_row = QHBoxLayout()
        row.addLayout(self._chip_row)
        row.addStretch(1)
        return self._wrap(row)

    def _install_shortcut(self) -> None:
        # Работать будет только если окно всё-таки получило фокус
        # (например, при `activateWindow()` из кода). Не рассчитываем.
        shortcut = QShortcut(QKeySequence("Esc"), self)
        shortcut.activated.connect(self.close)

    # --- стили ------------------------------------------------------------

    @staticmethod
    def _button_style(bg: str, hover: str, fg: str = "white") -> str:
        return f"""
            QPushButton {{
                background: {bg};
                color: {fg};
                border: none;
                border-radius: 8px;
                padding: 6px 12px;
                font-family: 'Segoe UI';
                font-size: 12px;
            }}
            QPushButton:hover {{ background: {hover}; }}
        """

    def _tab_style(self) -> str:
        accent = (f"rgb({_ACCENT.red()}, {_ACCENT.green()}, {_ACCENT.blue()})")
        return f"""
            QPushButton {{
                background: transparent;
                color: rgb({_MUTED.red()}, {_MUTED.green()}, {_MUTED.blue()});
                border: 1px solid rgba(78, 86, 102, 140);
                border-radius: 14px;
                padding: 5px 11px;
                font-family: 'Segoe UI';
                font-size: 11px;
            }}
            QPushButton:hover {{ color: white; }}
            QPushButton:checked {{
                background: rgba(61, 139, 253, 46);
                border: 1px solid {accent};
                color: white;
            }}
        """

    @staticmethod
    def _wrap(layout) -> QWidget:  # noqa: ANN001 - QHBoxLayout
        holder = QWidget()
        holder.setLayout(layout)
        holder.setStyleSheet("background: transparent;")
        return holder

    # --- данные -----------------------------------------------------------

    def set_data(self, data: PanelData) -> None:
        """Заполняет панель и показывает её."""
        first = data.groups[0][0] if data.groups else ""
        self._data = data

        self._summary.setText(f"{data.total} команд в {len(data.groups)} разделах")

        # Вкладки пересобираются: их набор зависит от реестра.
        for button in self._tab_buttons:
            button.deleteLater()
        self._tab_buttons.clear()
        for index, (key, title, count) in enumerate(data.groups):
            button = QPushButton(f"{title}  ·  {count}")
            button.setCheckable(True)
            button.setChecked(key == first)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(self._tab_style())
            button.clicked.connect(lambda _c, k=key: self.show_group(k))
            self._tabs.layout().addWidget(button)
            self._tab_buttons.append(button)

        self._fill_chips()
        if first:
            self.show_group(first)
        self._touch()

    def show_group(self, key: str) -> None:
        """Показывает карточки группы и включает её вкладку."""
        for index, (tab_key, _t, _c) in enumerate(self._data.groups):
            if index < len(self._tab_buttons):
                self._tab_buttons[index].setChecked(tab_key == key)

        # `deleteLater()` срабатывает через цикл событий, а мы
        # перерисовываем сетку сразу: между вызовами старые карточки
        # остаются на месте и просвечивают сквозь новые. `setParent(None)`
        # убирает виджет немедленно.
        while self._grid.count():
            widget = self._grid.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        cards = self._data.cards.get(key, [])
        for position, card in enumerate(cards):
            self._grid.addWidget(self._make_card(card), position // COLUMNS,
                                 position % COLUMNS)
        for column in range(COLUMNS):
            self._grid.setColumnStretch(column, 1)
        # Растягивать последний ряд нельзя: сетка растянет его по высоте
        # и между карточками и чипами появится пустая полоса. Ряды
        # должны быть ровно по высоте карточки.

        self._current_count = len(cards)
        self._fit_height(len(cards))
        self._touch()

    def _fill_chips(self) -> None:
        while self._chip_row.count():
            item = self._chip_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        # Кнопки держим списком: `findChildren` по layout'у не работает —
        # Qt переносит виджеты в родителя, а не в layout.
        self._chip_buttons: list[QPushButton] = []
        for card in self._data.chips:
            chip = QPushButton(card.safe_phrase)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip(f"Сказать: {card.safe_phrase}")
            chip.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(61, 139, 253, 32);
                    color: rgb(190, 214, 255);
                    border: 1px solid rgba(61, 139, 253, 120);
                    border-radius: 13px;
                    padding: 5px 12px;
                    font-family: 'Segoe UI';
                    font-size: 11px;
                }}
                QPushButton:hover {{ background: rgba(61, 139, 253, 74); color: white; }}
            """)
            chip.clicked.connect(
                lambda _c, p=card.safe_phrase: self._choose(p))
            self._chip_row.addWidget(chip)
            self._chip_buttons.append(chip)
        self._chip_row.addStretch(1)

    # --- карточка ---------------------------------------------------------

    def _make_card(self, card: Card) -> QWidget:
        box = QFrame()
        box.setFixedHeight(CARD_HEIGHT)
        edge = _DANGER if card.dangerous else _EDGE
        box.setStyleSheet(f"""
            QFrame {{
                background: rgb({_CARD.red()}, {_CARD.green()}, {_CARD.blue()});
                border: 1px solid rgba({edge.red()}, {edge.green()}, {edge.blue()}, 190);
                border-radius: 10px;
            }}
            QFrame:hover {{
                background: rgb({_CARD_HOVER.red()}, {_CARD_HOVER.green()}, {_CARD_HOVER.blue()});
            }}
        """)

        column = QVBoxLayout(box)
        column.setContentsMargins(13, 11, 13, 11)
        column.setSpacing(7)

        head = QHBoxLayout()
        icon = QLabel(card.icon)
        icon.setFont(QFont("Segoe UI Emoji", 13))
        head.addWidget(icon)

        title = QLabel(card.title)
        title.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        title.setWordWrap(True)
        title.setStyleSheet("color: #e8ebf0; border: none;")
        head.addWidget(title, 1)
        if card.dangerous:
            badge = QLabel("спросит")
            badge.setFont(QFont("Segoe UI", 8))
            badge.setStyleSheet("color: #e5534b; border: none;")
            head.addWidget(badge)
        column.addLayout(head)

        phrase = QLabel(f"«{card.safe_phrase}»")
        phrase.setWordWrap(True)
        phrase.setFont(QFont("Segoe UI", 10))
        phrase.setStyleSheet(
            f"color: rgb({_MUTED.red()}, {_MUTED.green()}, {_MUTED.blue()}); border: none;")
        column.addWidget(phrase)

        button = QPushButton("Попробовать")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(self._button_style(
            bg="rgba(61, 139, 253, 170)", hover="rgba(61, 139, 253, 230)"))
        button.clicked.connect(
            lambda _c=False, p=card.safe_phrase: self._choose(p))
        column.addWidget(button)

        return box

    # --- реакции ----------------------------------------------------------

    def _choose(self, phrase: str) -> None:
        """Человек нажал кнопку — говорим фразу ассистенту."""
        self._touch()
        log.info("Панель: отправляю фразу %r", phrase)
        self.phrase_chosen.emit(phrase)

    def _touch(self) -> None:
        """Любое действие перезапускает отсчёт бездействия."""
        self._idle.start()

    def _on_idle(self) -> None:
        log.info("Панель закрыта по бездействию (%d с)", int(IDLE_CLOSE_S))
        self.idle_closed.emit()
        self.close()

    def eventFilter(self, watched, event):  # noqa: ANN001, ANN201, N802
        """Движение мыши по панели — тоже действие человека."""
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.MouseMove:
            self._touch()
        return super().eventFilter(watched, event)

    def showEvent(self, event):  # noqa: ANN001, ANN201, N802
        super().showEvent(event)
        # Высоты шапки и ленты чипов известны только после показа, а
        # `resize()` до показа перебивается минимальными размерами
        # layout'а: окно оставалось прежней высоты и карточки висели
        # в пустоте. Поэтому подгоняем здесь.
        self._fit_height(self._current_count)
        self._touch()

    def _fit_height(self, card_count: int) -> None:
        """Подгоняет высоту окна под содержимое.

        Постоянная высота означала пустоту внизу у разделов с двумя
        карточками. Окно должно быть ровно такой, какая нужна.
        """
        rows = max(1, -(-card_count // COLUMNS))
        wanted = (self._header_height
                  + rows * CARD_HEIGHT + max(0, rows - 1) * GRID_SPACING
                  + self._chips_height
                  + self._vertical_padding)
        wanted = max(340, min(int(wanted), 900))
        if self.height() != wanted:
            self.resize(self.width() or 900, wanted)

    def keyPressEvent(self, event):  # noqa: ANN001, ANN201, N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)