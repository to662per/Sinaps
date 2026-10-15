# -*- coding: utf-8 -*-
"""Вкладка «История»: поиск, фильтры, статистика, выгрузка.

Отдельное окно истории с тех пор работает, но там ровно список. Здесь то,
ради чего человек историю и открывает: найти нужное из тысяч строк и
понять, как он пользуется ассистентом.

Запрос отдаётся наверх сигналом, а к базе отсюда не ходим: база живёт в
asyncio-потоке, а окно — в Qt-потоке, и обращение из одного в другой без
границы кончается либо зависанием, либо порчей соединения.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout, QWidget)

from .theme import (CARD, DANGER, EDGE, MUTED, TEXT, button, color, font,
                    label, rgba, shell_style)

#: Сколько строк показывать за раз. Больше — и список перестаёт читаться
#: глазами, меньше — и поиск приходится повторять.
PAGE_SIZE = 300


def human_time(ts: float) -> str:
    """Время для списка: сегодня — часами, иначе — датой."""
    moment = time.localtime(ts)
    today = time.localtime()
    if moment.tm_yday == today.tm_yday and moment.tm_year == today.tm_year:
        return time.strftime("%H:%M", moment)
    if moment.tm_year == today.tm_year:
        return time.strftime("%d.%m %H:%M", moment)
    return time.strftime("%d.%m.%Y", moment)


class HistoryView(QWidget):
    """Список истории с фильтрами."""

    #: Нужен обновлённый список. Проброс в ассистент.
    query_changed = Signal(dict)
    #: Попросили выгрузить: (путь, фильтры).
    export_requested = Signal(str, dict)
    #: Очистить историю — с подтверждением голосом.
    clear_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list = []
        self._build()

    # --- построение -------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(self._build_filters())
        layout.addWidget(self._build_summary(), 0)
        layout.addWidget(self._build_list(), 1)
        layout.addWidget(self._build_actions())

    def _build_filters(self) -> QWidget:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Поиск по репликам…")
        self._search.setFont(font(10))
        self._search.setStyleSheet(self._input_style())
        # Ищем по нажатию Enter, а не на каждую букву: список большой,
        # и перерисовка на каждое нажатие заметно тормозит.
        self._search.returnPressed.connect(self._emit_query)
        row.addWidget(self._search, 1)

        self._role = QComboBox()
        self._role.addItems(["Кто угодно", "Вы", "Син"])
        self._role.setStyleSheet(self._combo_style())
        self._role.currentIndexChanged.connect(self._emit_query)
        row.addWidget(self._role)

        self._intent = QComboBox()
        self._intent.setEditable(True)
        self._intent.setPlaceholderText("Намерение")
        self._intent.setStyleSheet(self._combo_style())
        self._intent.currentTextChanged.connect(self._emit_query)
        row.addWidget(self._intent)

        return self._wrap(row)

    def _build_summary(self) -> QWidget:
        box = QFrame()
        box.setObjectName("history_summary")
        box.setStyleSheet(shell_style("history_summary"))

        self._summary = QLabel("История пуста")
        self._summary.setFont(font(9))
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(label(size=9, tone=MUTED))
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(12, 10, 12, 10)
        box_layout.addWidget(self._summary)

        self._stats = QLabel("")
        self._stats.setFont(font(8))
        self._stats.setWordWrap(True)
        self._stats.setStyleSheet(label(size=8, tone=MUTED))
        box_layout.addWidget(self._stats)

        return box

    def _build_list(self) -> QWidget:
        self._list = QListWidget()
        self._list.setFont(font(9))
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: {rgba(CARD, 255)};
                border: 1px solid {rgba(EDGE, 160)};
                border-radius: 8px;
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 6px 8px;
                border-radius: 6px;
                color: {color(TEXT)};
            }}
            QListWidget::item:selected {{
                background: {rgba(EDGE, 120)};
            }}
            QListWidget::item:hover {{
                background: {rgba(EDGE, 70)};
            }}
        """)
        return self._list

    def _build_actions(self) -> QWidget:
        row = QHBoxLayout()
        row.setSpacing(8)

        export = QPushButton("Выгрузить…")
        export.setCursor(Qt.CursorShape.PointingHandCursor)
        export.setStyleSheet(button(
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 160)",
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 255)"))
        export.clicked.connect(self._emit_export)
        row.addWidget(export)

        clear = QPushButton("Очистить историю")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.setStyleSheet(button(
            "rgba(0, 0, 0, 0)",
            f"rgba({DANGER.red()}, {DANGER.green()}, {DANGER.blue()}, 180)"))
        # Подтверждается голосом: стирание 30 дней разговоров кнопкой
        # означало бы, что человек решил это за один щелчок.
        clear.clicked.connect(self.clear_requested)
        row.addWidget(clear)

        row.addStretch(1)
        return self._wrap(row)

    # --- данные -----------------------------------------------------------

    def set_entries(self, entries) -> None:
        """Показать записи."""
        self._entries = list(entries)
        self._list.clear()

        if not self._entries:
            self._list.addItem("Ничего не найдено")
            self._list.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
            return

        for entry in self._entries:
            item = QListWidgetItem(self._entry_line(entry))
            item.setToolTip(entry.text)
            if entry.role == "assistant":
                item.setForeground(MUTED)
            self._list.addItem(item)

    def set_stats(self, stats: dict) -> None:
        """Показать сводку за неделю.

        Показывается только то, что человек может с этим сделать:
        «сколько фраз» и «топ намерений». Средняя задержка — для
        настройки, ей место в разделе «Состояние».
        """
        if not stats:
            self._stats.setText("")
            return

        turns = int(stats.get("turns", 0))
        phrases = int(stats.get("phrases", 0))
        top = stats.get("top_intents") or []

        parts = [f"за {stats.get('days', 7)} дн.: обменов {turns}, "
                 f"реплик {phrases}"]
        if top:
            top_text = ", ".join(
                f"{item['intent']} — {item['count']}" for item in top)
            parts.append(f"топ намерений: {top_text}")
        self._stats.setText("\n".join(parts))

    def set_summary(self, text: str) -> None:
        self._summary.setText(text)

    @staticmethod
    def _entry_line(entry) -> str:
        who = "Вы" if entry.role == "user" else "Син"
        text = (entry.text or "").replace("\n", " ").strip()
        if len(text) > 90:
            text = text[:89] + "…"
        intent = f"  [{entry.intent}]" if entry.intent else ""
        return f"{human_time(entry.ts)}  {who}: {text}{intent}"

    # --- фильтры ----------------------------------------------------------

    def _emit_query(self) -> None:
        role = {0: "", 1: "user", 2: "assistant"}.get(
            self._role.currentIndex(), "")
        self.query_changed.emit({
            "query": self._search.text().strip(),
            "role": role,
            "intent": self._intent.currentText().strip(),
        })

    def _emit_export(self) -> None:
        role = {0: "", 1: "user", 2: "assistant"}.get(
            self._role.currentIndex(), "")
        self.export_requested.emit("", {
            "query": self._search.text().strip(),
            "role": role,
            "intent": self._intent.currentText().strip(),
        })

    def refresh(self) -> None:
        """Перезапросить по текущим фильтрам."""
        self._emit_query()

    @staticmethod
    def _wrap(layout) -> QWidget:
        box = QWidget()
        box.setLayout(layout)
        return box

    @staticmethod
    def _input_style() -> str:
        return f"""
            QLineEdit {{
                background: {rgba(CARD, 255)};
                border: 1px solid {rgba(EDGE, 200)};
                border-radius: 8px;
                padding: 7px 10px;
                color: {color(TEXT)};
                font-size: 10pt;
            }}
            QLineEdit:focus {{ border-color: {rgba(EDGE, 255)}; }}
        """

    @staticmethod
    def _combo_style() -> str:
        return f"""
            QComboBox {{
                background: {rgba(CARD, 255)};
                border: 1px solid {rgba(EDGE, 200)};
                border-radius: 8px;
                padding: 5px 8px;
                color: {color(TEXT)};
                min-width: 110px;
            }}
        """


__all__ = ["PAGE_SIZE", "HistoryView", "human_time"]