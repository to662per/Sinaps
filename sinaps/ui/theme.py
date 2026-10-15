# -*- coding: utf-8 -*-
"""Цвета и общие мелочи оформления окон.

Вынесено из `panel.py`, потому что главное окно, панель и список
плагинов должны выглядеть одинаково. Пока каждый рисовал своё, цвета
разъезжались на полутон при каждом новом окне, и три окна выглядели как
три разных программы.

Значения — те же, что в `panel.py` до этого изменения. Ничего не
подкручивалось «по вкусу»: если окна расходятся, правится здесь, а не
в каждом файле отдельно.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont

#: Фон окон и карточек.
BACKDROP = QColor(18, 20, 26, 244)
SHELL = QColor(24, 27, 34, 252)
CARD = QColor(32, 36, 45, 255)
CARD_HOVER = QColor(44, 50, 62, 255)
EDGE = QColor(78, 86, 102, 255)

#: Текст.
TEXT = QColor(232, 235, 240)
MUTED = QColor(154, 164, 178)

#: Акценты.
ACCENT = QColor(61, 139, 253)
SUCCESS = QColor(87, 191, 122)
WARNING = QColor(232, 176, 65)
DANGER = QColor(229, 83, 75)

#: Цвета статусов плагина. Совпадают с треем, чтобы «спит» в окне и
#: «спит» в трее выглядели одинаково.
STATUS_COLORS = {
    "loaded": SUCCESS,
    "asleep": MUTED,
    "error": DANGER,
    "disabled": MUTED,
}

#: Шрифт по умолчанию для всего интерфейса. Раньше каждый виджет
#: просил Segoe UI отдельно, и список прав в окне плагинов вышел бы
#: другим шрифтом, чем заголовок рядом.
FONT_FAMILY = "Segoe UI"


def color(c: QColor) -> str:
    """`QColor` → строка для таблицы стилей Qt."""
    return f"rgb({c.red()}, {c.green()}, {c.blue()})"


def rgba(c: QColor, alpha: int) -> str:
    """`QColor` с произвольной прозрачностью → строка для Qt."""
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def font(size: int = 10, bold: bool = False) -> QFont:
    """Шрифт интерфейса.

    Единая точка вместо `QFont("Segoe UI", 10)` в каждом виджете:
    смена семейства в одном месте, а не поиск по файлам.
    """
    weight = QFont.Weight.DemiBold if bold else QFont.Weight.Normal
    return QFont(FONT_FAMILY, size, weight)


def label(*, size: int = 10, bold: bool = False,
          tone: QColor | None = None) -> str:
    """Стиль текста для `QLabel`.

    Текст сюда не передаётся: он задаётся виджету отдельно. Стиль с
    текстом внутри путаешь, когда правишь подпись — а подпись меняется
    часто, стиль почти никогда.
    """
    chosen = TEXT if tone is None else tone
    return (f"color: {color(chosen)}; "
            f"font-family: '{FONT_FAMILY}'; "
            f"font-size: {size}pt;"
            + (" font-weight: 600;" if bold else ""))


def button(bg: str, hover: str, fg: str = "white", radius: int = 8) -> str:
    """Готовый стиль для `QPushButton`."""
    return f"""
        QPushButton {{
            background: {bg};
            color: {fg};
            border: none;
            border-radius: {radius}px;
            padding: 7px 14px;
            font-family: '{FONT_FAMILY}';
            font-size: 10pt;
        }}
        QPushButton:hover {{ background: {hover}; }}
        QPushButton:disabled {{ color: {color(MUTED)}; }}
    """


def shell_style(object_name: str = "shell") -> str:
    """Стиль оболочки окна: рамка и скругление."""
    return f"""
        #{object_name} {{
            background: {rgba(SHELL, 250)};
            border: 1px solid {rgba(EDGE, 220)};
            border-radius: 12px;
        }}
    """


__all__ = [
    "ACCENT", "BACKDROP", "CARD", "CARD_HOVER", "DANGER", "EDGE",
    "FONT_FAMILY", "MUTED", "SHELL", "STATUS_COLORS", "SUCCESS",
    "TEXT", "WARNING", "button", "color", "font", "label",
    "rgba", "shell_style",
]