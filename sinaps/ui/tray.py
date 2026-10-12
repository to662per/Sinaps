"""Трей: индикатор состояния и меню.

Иконка рисуется кодом (`QPixmap` + `QPainter`), а не грузится из файла.
Причины практические:
  * состояний много, менять их должно быть можно без дизайнера;
  * нечего таскать рядом с программой файлы `.ico` для каждого цвета;
  * на любом DPI рисуется чётко — Qt сам масштабирует.

Цвет состояния
--------------
    серый    спит (микрофон открыт, но ждёт активации)
    зелёный  слушает
    жёлтый   думает
    синий    говорит
    красный  ошибка
    тусклый  пауза

Потоки
------
Qt требует главный поток, asyncio крутится в отдельном. `Bridge` живёт
в Qt-потоке и служит единственной точкой обмена: из asyncio в Qt идут
сигналы (Qt сам разложит их в очередь), из Qt в asyncio — вызовы
через `loop.call_soon_threadsafe`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QCursor, QIcon, QPainter,
                           QPen, QPixmap)
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

log = logging.getLogger(__name__)


#: Состояние сессии -> (цвет, подпись, пояснение).
STATES: dict[str, tuple[str, str, str]] = {
    "idle":         ("#9aa4b2", "Спит",        "Микрофон открыт, жду активации"),
    "listening":    ("#2fbf71", "Слушаю",     "Идёт запись"),
    "transcribing": ("#2fbf71", "Распознаю",  "Разбираю речь"),
    "thinking":     ("#f2b134", "Думаю",      "Понимаю намерение"),
    "routing":      ("#f2b134", "Выполняю",   "Запускаю навык"),
    "speaking":     ("#3d8bfd", "Говорю",     "Озвучиваю ответ"),
    "confirming":   ("#f2b134", "Жду ответа", "Нужно подтверждение"),
    "error":        ("#e5534b", "Ошибка",     "Что-то пошло не так"),
    "paused":       ("#5c636e", "Пауза",      "Не реагирую на голос"),
}


@dataclass(frozen=True)
class TrayView:
    """Всё, что трею нужно знать о текущем моменте."""

    state: str = "idle"
    detail: str = ""
    listening: bool = True
    speak_enabled: bool = True
    history_count: int = 0


def paint_icon(state: str, *, size: int = 64) -> QIcon:
    """Круг состояния с тремя штрихами — читается и в 16 пикселей."""
    color = STATES.get(state, STATES["idle"])[0]
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        margin = size * 0.08
        painter.setPen(QPen(QColor(0, 0, 0, 0)))
        painter.setBrush(QBrush(QColor(color)))
        painter.drawEllipse(int(margin), int(margin), int(size - 2 * margin),
                            int(size - 2 * margin))

        pen = QPen(QColor(255, 255, 255, 235), max(1.0, size * 0.075))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 0)))

        centre = size / 2.0
        # Радиус круга = (size - 2*margin)/2. Штрихи должны оставаться
        # внутри с запасом, иначе на 64 px они упираются в обводку.
        span = size * 0.13
        for index, offset in enumerate((-0.27, 0.0, 0.27)):
            x = centre + offset * size
            half = span * (1.0 if index == 1 else 0.60)
            painter.drawLine(int(x), int(centre - half),
                             int(x), int(centre + half))
    finally:
        painter.end()

    return QIcon(pixmap)


class Bridge(QObject):
    """Единственная точка обмена между Qt-потоком и asyncio-потоком.

    Сигналы живут здесь, а не в `QSystemTrayIcon`, потому что Qt выбирает
    тип соединения (прямое или через очередь) по потокам при `connect`,
    и приёмник обязан жить в том же потоке, что и сам объект.
    """

    status_changed = Signal(str, str)      # состояние, пояснение
    view_changed = Signal(object)           # TrayView
    pause_toggled = Signal(bool)
    speak_toggled = Signal(bool)
    history_requested = Signal()
    settings_requested = Signal()
    capabilities_requested = Signal()
    #: Ассистент попросил открыть панель. Испускается из asyncio-потока,
    #: поэтому ходить через сигнал, а не через `QTimer.singleShot`:
    #: таймер создаётся в том потоке, из которого вызван, и в чужом
    #: потоке без Qt-цикла просто не срабатывает.
    panel_requested = Signal()
    clear_history_requested = Signal()
    quit_requested = Signal()
    status_window_requested = Signal()


class TrayIcon(QSystemTrayIcon):
    """Иконка в трее с меню действий."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.bridge = Bridge(self)
        self._view = TrayView()
        self._build_menu()

        self.setIcon(paint_icon("idle"))
        self.setToolTip("Sinaps — Спит")

        self.activated.connect(self._on_activated)
        self.bridge.status_changed.connect(self._on_status)
        self.bridge.view_changed.connect(self._on_view)

    # --- меню -------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = QMenu()

        self._status_action = QAction("Спит", menu)
        self._status_action.setEnabled(False)
        menu.addAction(self._status_action)

        self._detail_action = QAction("", menu)
        self._detail_action.setEnabled(False)
        menu.addAction(self._detail_action)

        self._history_action = QAction("История: 0", menu)
        self._history_action.setEnabled(False)
        menu.addAction(self._history_action)
        menu.addSeparator()

        self._pause_action = QAction("Пауза", menu)
        self._pause_action.setCheckable(True)
        self._pause_action.setToolTip("Не реагировать на голос")
        self._pause_action.toggled.connect(self.bridge.pause_toggled)
        menu.addAction(self._pause_action)

        self._mute_action = QAction("Озвучка ответа", menu)
        self._mute_action.setCheckable(True)
        self._mute_action.setChecked(True)
        self._mute_action.toggled.connect(self.bridge.speak_toggled)
        menu.addAction(self._mute_action)
        menu.addSeparator()

        history_action = QAction("История…", menu)
        history_action.triggered.connect(self.bridge.history_requested)
        menu.addAction(history_action)

        # Панель нужна не только голосу: по требованию пропала может
        # оказаться любая микрофонная кнопка, и тогда спросить нечем.
        capabilities_action = QAction("Что я умею…", menu)
        capabilities_action.setToolTip("Показать панель с примерами команд")
        capabilities_action.triggered.connect(
            self.bridge.capabilities_requested)
        menu.addAction(capabilities_action)

        settings_action = QAction("Настройки…", menu)
        settings_action.triggered.connect(self.bridge.settings_requested)
        menu.addAction(settings_action)

        clear_action = QAction("Очистить историю", menu)
        clear_action.triggered.connect(self.bridge.clear_history_requested)
        menu.addAction(clear_action)
        menu.addSeparator()

        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self.bridge.quit_requested)
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self._menu = menu

    # --- реакция на события -----------------------------------------------

    def _on_status(self, state: str, detail: str) -> None:
        _, title, default_detail = STATES.get(state, STATES["idle"])
        self.setIcon(paint_icon("paused" if not self._view.listening else state))
        self.setToolTip(f"Sinaps — {title}")
        self._status_action.setText(title)
        self._detail_action.setText(detail or default_detail)

    def _on_view(self, view) -> None:
        """Полное состояние: пауза, озвучка, размер истории."""
        if not isinstance(view, TrayView):
            return
        self._view = view

        self.setIcon(paint_icon("paused" if not view.listening else view.state))
        _, title, _ = STATES.get(view.state, STATES["idle"])
        self.setToolTip(f"Sinaps — {'Пауза' if not view.listening else title}")

        # Блокируем сигналы: программная установка галочки не должна
        # выглядеть как действие пользователя и слать сигнал обратно.
        for action, value in ((self._pause_action, not view.listening),
                              (self._mute_action, view.speak_enabled)):
            action.blockSignals(True)
            action.setChecked(value)
            action.blockSignals(False)

        self._history_action.setText(f"История: {view.history_count}")

    def _on_activated(self, reason) -> None:
        reason = QSystemTrayIcon.ActivationReason(reason)
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.bridge.status_window_requested.emit()
        else:
            self._menu.popup(QCursor.pos())

    # --- доступ извне -----------------------------------------------------

    def current_view(self) -> TrayView:
        return self._view

    def show_message(self, title: str, body: str, *, warning: bool = False) -> None:
        """Всплывающее уведомление. Тихо игнорируется, если трей недоступен."""
        if not QSystemTrayIcon.supportsMessages():
            return
        icon = (QSystemTrayIcon.MessageIcon.Warning if warning
                else QSystemTrayIcon.MessageIcon.Information)
        self.showMessage(title, body, icon, 4000)