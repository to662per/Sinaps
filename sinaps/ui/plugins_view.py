# -*- coding: utf-8 -*-
"""Вкладка «Плагины»: список карточек с состоянием и правами.

Окно плагинов — первое, где человек видит, что происходит с тем, что
сам же и устанавливал. Поэтому здесь три вещи сделаны не «как удобно
программисту», а как понятно человеку:

1. **Статус виден словами, а не цветом.** «активен», «спит», «ошибка» —
   цвет рядом, но текст главный: различать цвета приходится тем, кто
   и так не различает.

2. **Право показывается по-русски.** `platform.power` человеку ничего не
   говорит. Пояснение берётся из `manifest.permission_title`.

3. **Память показывается честно.** У спящего плагина процесса нет, и в
   карточке стоит прочерк, а не его прошлая память: показывать память
   закрытого процесса — значит врать.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from ..plugins.view import (STATUS_ASLEEP, STATUS_DISABLED, STATUS_ERROR,
                            STATUS_LOADED, PluginCard, PluginView)
from .theme import (CARD, CARD_HOVER, DANGER, EDGE, MUTED, STATUS_COLORS,
                    SUCCESS, TEXT, button, color, font, label, rgba,
                    shell_style)


class PluginRow(QFrame):
    """Карточка одного плагина."""

    #: Нажали «Перезагрузить».
    reload_requested = Signal(str)
    #: Нажали «Одобрить права».
    approve_requested = Signal(str)
    #: Нажали «Отозвать права».
    revoke_requested = Signal(str)
    #: Нажали «Выключить» / «Включить».
    toggle_requested = Signal(str, bool)

    def __init__(self, card: PluginCard, parent: QWidget | None = None):
        super().__init__(parent)
        self._card = card

        self.setObjectName("plugin_card")
        self.setStyleSheet(f"""
            #plugin_card {{
                background: {rgba(CARD, 255)};
                border: 1px solid {rgba(EDGE, 180)};
                border-radius: 10px;
            }}
            #plugin_card:hover {{
                background: {rgba(CARD_HOVER, 255)};
                border-color: {rgba(EDGE, 230)};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        layout.addLayout(self._header())
        layout.addWidget(self._description())

        if card.permissions:
            layout.addWidget(self._permissions())

        if card.error:
            layout.addWidget(self._error())
        else:
            layout.addWidget(self._actions())

    # --- части карточки ----------------------------------------------------

    def _header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        title = QLabel(self._card.name)
        title.setFont(font(11, bold=True))
        title.setStyleSheet(label(size=11, bold=True))
        row.addWidget(title)

        version = QLabel(f"v{self._card.version}")
        version.setFont(font(8))
        version.setStyleSheet(label(size=8, tone=MUTED))
        row.addWidget(version)

        row.addStretch(1)

        memory = QLabel(self._card.memory_label)
        memory.setFont(font(9))
        memory.setStyleSheet(label(size=9, tone=MUTED))
        memory.setToolTip("Сколько памяти занимает процесс плагина")
        row.addWidget(memory)

        status = QLabel(self._status_label())
        status.setFont(font(9, bold=True))
        tone = STATUS_COLORS.get(self._card.status, MUTED)
        status.setStyleSheet(f"color: {color(tone)}; font-size: 9pt;")
        row.addWidget(status)

        return row

    def _status_label(self) -> str:
        """Статус словами, а не только цветом.

        Прочерк в конце — подсказка, что память считается только у
        живых: у спящего плагина «—» вместо числа.
        """
        return self._card.status_title

    def _description(self) -> QWidget:
        box = QLabel(self._card.description or "—")
        box.setFont(font(9))
        box.setStyleSheet(label(size=9, tone=MUTED))
        box.setWordWrap(True)
        box.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        return box

    def _permissions(self) -> QWidget:
        """Список прав с пояснениями.

        Каждое право — строка, а не один склеенный перечень: список из
        трёх прав в одну строку читается как одно длинное слово, и
        человек не понимает, сколько их там на самом деле.
        """
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 2, 0, 2)
        column.setSpacing(3)

        caption = QLabel("Права:")
        caption.setFont(font(8))
        caption.setStyleSheet(label(size=8, tone=MUTED))
        column.addWidget(caption)

        for permission in self._card.permissions:
            column.addWidget(self._permission_line(permission))
        return box

    def _permission_line(self, permission) -> QWidget:
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)

        mark = QLabel("✓" if permission.granted else "•")
        mark.setFont(font(9, bold=True))
        tone = SUCCESS if permission.granted else DANGER
        mark.setStyleSheet(f"color: {color(tone)}; font-size: 9pt;")
        mark.setFixedWidth(12)
        line.addWidget(mark)

        text = QLabel(permission.title)
        text.setFont(font(9))
        text.setStyleSheet(label(size=9, tone=MUTED if not permission.granted
                               else TEXT))
        text.setToolTip(f"Имя права: {permission.name}")
        line.addWidget(text, 1)

        revoke = QPushButton("отозвать")
        revoke.setFixedHeight(22)
        revoke.setCursor(Qt.CursorShape.PointingHandCursor)
        revoke.setStyleSheet(button(
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 160)",
            f"rgba({DANGER.red()}, {DANGER.green()}, {DANGER.blue()}, 200)",
            radius=6))
        revoke.clicked.connect(
            lambda: self.revoke_requested.emit(self._card.name))
        line.addWidget(revoke)

        return row

    def _error(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        reason = QLabel(self._card.error)
        reason.setFont(font(9))
        reason.setStyleSheet(label(size=9, tone=DANGER))
        reason.setWordWrap(True)
        reason.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        row.addWidget(reason, 1)

        return box

    def _actions(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(8)

        if self._card.commands:
            commands = QLabel(
                f"{len(self._card.commands)} команд: "
                + ", ".join(c.split(".", 1)[-1] for c in self._card.commands))
            commands.setFont(font(8))
            commands.setStyleSheet(label(size=8, tone=MUTED))
            commands.setToolTip("\n".join(self._card.commands))
            row.addWidget(commands, 1)
        else:
            row.addStretch(1)

        if self._card.needs_approval:
            approve = QPushButton("Одобрить права")
            approve.setCursor(Qt.CursorShape.PointingHandCursor)
            approve.setStyleSheet(button(
                f"rgba({DANGER.red()}, {DANGER.green()}, {DANGER.blue()}, 200)",
                f"rgba({DANGER.red()}, {DANGER.green()}, {DANGER.blue()}, 255)"))
            approve.clicked.connect(
                lambda: self.approve_requested.emit(self._card.name))
            row.addWidget(approve)

        if self._card.can_reload:
            reload_button = QPushButton("Перезагрузить")
            reload_button.setCursor(Qt.CursorShape.PointingHandCursor)
            reload_button.setStyleSheet(button(
                f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 160)",
                f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 255)"))
            reload_button.setToolTip(
                "Поднять заново, не перезапуская ассистент")
            reload_button.clicked.connect(
                lambda: self.reload_requested.emit(self._card.name))
            row.addWidget(reload_button)

        toggle = QPushButton("Выключить" if self._card.status
                             != STATUS_DISABLED else "Включить")
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setStyleSheet(button(
            "rgba(0, 0, 0, 0)",
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 160)"))
        toggle.clicked.connect(
            lambda: self.toggle_requested.emit(
                self._card.name, self._card.status == STATUS_DISABLED))
        row.addWidget(toggle)

        return box


class PluginsView(QWidget):
    """Список плагинов с прокруткой."""

    reload_requested = Signal(str)
    approve_requested = Signal(str)
    revoke_requested = Signal(str)
    toggle_requested = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.set_view(PluginView())

    def set_view(self, view: PluginView) -> None:
        """Показать снимок.

        Виджеты пересоздаются целиком, а не обновляются на месте: карточек
        обычно две-три, а состояние у каждой своё. Обновление на месте
        требовало бы помнить про каждый виджет, и забытый угол дал бы
        устаревшую надпись, которую не заметить.
        """
        layout = self.layout() or self._ensure_layout()
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not view.cards:
            layout.addWidget(self._empty())
            return

        for card in view.cards:
            row = PluginRow(card)
            row.reload_requested.connect(self.reload_requested)
            row.approve_requested.connect(self.approve_requested)
            row.revoke_requested.connect(self.revoke_requested)
            row.toggle_requested.connect(self.toggle_requested)
            layout.addWidget(row)

        layout.addStretch(1)

    def _ensure_layout(self) -> QVBoxLayout:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        return outer

    def _empty(self) -> QWidget:
        box = QFrame()
        box.setObjectName("plugins_empty")
        box.setStyleSheet(shell_style("plugins_empty"))
        box.setMinimumHeight(220)

        column = QVBoxLayout(box)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.setSpacing(8)

        title = QLabel("Плагинов нет")
        title.setFont(font(12, bold=True))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(label(size=12, bold=True))
        column.addWidget(title)

        hint = QLabel(
            "Положи папку с manifest.yaml и кодом в plugins/ — "
            "плагин подхватится при следующем запуске.")
        hint.setFont(font(9))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet(label(size=9, tone=MUTED))
        column.addWidget(hint)

        return box


__all__ = ["PluginRow", "PluginsView", "STATUS_ASLEEP", "STATUS_ERROR",
           "STATUS_LOADED"]