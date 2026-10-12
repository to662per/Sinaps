"""Окна: история диалогов и настройки.

Окна живут в Qt-потоке, а данные берутся из хранилища и конфига.
Долгих операций здесь нет — SQLite открывается на запрос и закрывается,
поэтому блокировать интерфейс незачем.

Что показывать в настройках
---------------------------
Не всё. Показываем то, что человек реально меняет: микрофон, голос,
кнопку активации, модель распознавания, автозапуск. Остальное живёт
в `config.yaml` — это осознанно, комментарии там полезнее, чем
галочки в форме.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialogButtonBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QSizePolicy,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

from ..core.config import save_config

log = logging.getLogger(__name__)


# --- вспомогательное -------------------------------------------------------


def _fmt_day(ts: float) -> str:
    moment = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    day = moment.date()
    if day == today:
        return "Сегодня"
    if day == today.fromordinal(today.toordinal() - 1):
        return "Вчера"
    return moment.strftime("%d.%m.%Y")


def _humanize(entry) -> str:
    """Подпись реплики для списка."""
    who = "Вы" if entry.role == "user" else "Синапс"
    text = " ".join(entry.text.split())
    if len(text) > 120:
        text = text[:117] + "..."
    return f"{entry.when}  {who}: {text}"


def _localize_buttons(box: QDialogButtonBox) -> None:
    """Подписи кнопок по-русски.

    Qt берёт их из своего локализованного каталога, а у нас в сборке
    только английский. Для пользователя английский интерфейс в русском
    ассистенте выглядит как недоделка, поэтому подписи задаём явно.
    """
    labels = {
        QDialogButtonBox.StandardButton.Save: "Сохранить",
        QDialogButtonBox.StandardButton.Cancel: "Отмена",
        QDialogButtonBox.StandardButton.Ok: "ОК",
        QDialogButtonBox.StandardButton.Close: "Закрыть",
        QDialogButtonBox.StandardButton.Yes: "Да",
        QDialogButtonBox.StandardButton.No: "Нет",
    }
    for standard, text in labels.items():
        button = box.button(standard)
        if button is not None:
            button.setText(text)


# --- история ---------------------------------------------------------------


class HistoryWindow(QWidget):
    """Просмотр прошлых разговоров с поиском."""

    def __init__(self, store, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("Sinaps — история")
        self.resize(900, 560)

        layout = QVBoxLayout(self)

        # --- поиск ---
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Поиск:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("например: блокнот")
        self._search.textChanged.connect(self._reload)
        search_row.addWidget(self._search)

        self._refresh = QPushButton("Обновить")
        self._refresh.clicked.connect(self._reload)
        search_row.addWidget(self._refresh)

        self._clear = QPushButton("Очистить всё")
        self._clear.clicked.connect(self._clear_all)
        search_row.addWidget(self._clear)
        layout.addLayout(search_row)

        # --- список и подробности ---
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._show_detail)
        splitter.addWidget(self._list)

        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setPlaceholderText("Выберите реплику слева")
        splitter.addWidget(self._detail)

        splitter.setSizes([520, 380])
        # Список и подробности обязаны занять всю высоту окна, иначе
        # снизу остаётся мёртвая полоса, а реплики приходится искать
        # прокруткой в узкой полоске.
        self._list.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Expanding)
        self._detail.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Expanding)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        self._status = QLabel("")
        layout.addWidget(self._status)

        self._entries: list = []
        self._reload()

    def _reload(self) -> None:
        needle = self._search.text().strip()
        try:
            self._entries = (self._store.search(needle, limit=500) if needle
                             else self._store.recent(limit=500))
        except Exception as exc:            # история не должна ронять окно
            log.warning("История: не удалось прочитать (%s)", exc)
            self._entries = []

        self._list.clear()
        last_day = None
        for entry in self._entries:
            day = _fmt_day(entry.ts)
            if day != last_day:
                header = QListWidgetItem(f"— {day} —")
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                font = header.font()
                font.setBold(True)
                header.setFont(font)
                self._list.addItem(header)
                last_day = day

            item = QListWidgetItem(_humanize(entry))
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            if entry.role != "user":
                item.setForeground(Qt.GlobalColor.darkGray)
            self._list.addItem(item)

        total = self._store.stats().get("total", 0)
        self._status.setText(f"Показано {len(self._entries)} из {total} реплик")

    def _show_detail(self, row: int) -> None:
        item = self._list.item(row) if row >= 0 else None
        if item is None:
            self._detail.clear()
            return
        entry_id = item.data(Qt.ItemDataRole.UserRole)
        if entry_id is None:
            return

        for entry in self._entries:
            if entry.id == entry_id:
                when = datetime.fromtimestamp(entry.ts)
                lines = [
                    f"Роль        : {'пользователь' if entry.role == 'user' else 'ассистент'}",
                    f"Время       : {when.strftime('%d.%m.%Y %H:%M:%S')}",
                    f"Источник    : {entry.source or '—'}",
                    f"Уверенность : {entry.confidence:.2f}",
                    f"Намерение   : {entry.intent or '—'}",
                    f"Задержка    : {entry.latency_ms} мс" if entry.latency_ms else "Задержка    : —",
                    "",
                    "Текст:",
                    entry.text,
                ]
                if entry.meta:
                    lines += ["", "Метаданные:", str(entry.meta)]
                self._detail.setPlainText("\n".join(lines))
                return

    def _clear_all(self) -> None:
        answer = QMessageBox.question(
            self, "Очистить историю",
            "Удалить всю сохранённую историю разговоров?\n"
            "Действие необратимо.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._store.clear()
        self._reload()


# --- настройки -------------------------------------------------------------


class SettingsWindow(QWidget):
    """Настройки, которые меняют люди, а не разработчики.

    `on_apply` вызывается после сохранения: там главный поток решает,
    что можно применить сразу (пауза, озвучка), а что требует перезапуска
    (модель распознавания, микрофон).
    """

    def __init__(
        self,
        cfg,
        *,
        audio_devices: Callable[[], list[tuple[Any, str]]] | None = None,
        voices: Callable[[], list[str]] | None = None,
        models: list[str] | None = None,
        on_apply: Callable[[dict], None] | None = None,
        autostart_state: Callable[[], bool] | None = None,
        on_autostart: Callable[[bool], bool] | None = None,
        on_mic_changed: Callable[[Any], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._on_apply = on_apply
        self._on_autostart = on_autostart
        self._on_mic_changed = on_mic_changed

        self.setWindowTitle("Sinaps — настройки")
        self.resize(640, 520)

        tabs = QTabWidget()
        self._build_audio_tab(tabs, audio_devices)
        self._build_voice_tab(tabs, voices)
        self._build_advanced_tab(tabs, models, autostart_state)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        _localize_buttons(buttons)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    # --- вкладки ----------------------------------------------------------

    def _build_audio_tab(self, tabs: QTabWidget, devices) -> None:
        box = QGroupBox("Звук")
        form = QFormLayout(box)

        self._mic = QComboBox()
        self._mic.addItem("Системный по умолчанию", None)
        if devices is not None:
            try:
                for index, name in devices():
                    self._mic.addItem(name, index)
            except Exception as exc:
                log.warning("Настройки: не удалось получить устройства (%s)", exc)
        self._mic.setCurrentIndex(max(0, self._mic.findData(
            self._cfg.audio.input_device)))
        self._mic.currentIndexChanged.connect(self._preview_mic)
        form.addRow("Микрофон ввода:", self._mic)

        self._speaking = QCheckBox("Озвучивать ответы голосом")
        self._speaking.setChecked(True)
        form.addRow(self._speaking)

        self._quiescence = QLineEdit(str(self._cfg.vad.silence_ms))
        self._quiescence.setToolTip(
            "Пауза, которая считается концом фразы.\n"
            "Меньше — отвечает быстрее, но может отрезать последнее слово.")
        form.addRow("Конец фразы, мс:", self._quiescence)

        self._timeout = QLineEdit(str(self._cfg.wake.listen_timeout_s))
        self._timeout.setToolTip("Сколько ждать речи после нажатия кнопки")
        form.addRow("Окно ожидания речи, с:", self._timeout)

        tabs.addTab(self._wrap(box), "Звук")

    def _build_voice_tab(self, tabs: QTabWidget, voices) -> None:
        box = QGroupBox("Голос и активация")
        form = QFormLayout(box)

        self._voice = QComboBox()
        self._voice.setEditable(True)
        if voices is not None:
            try:
                for name in voices():
                    self._voice.addItem(name)
            except Exception as exc:
                log.warning("Настройки: не удалось получить голоса (%s)", exc)
        self._voice.setCurrentText(self._cfg.tts.voice or "")
        self._voice.setToolTip("Пусто — голос по умолчанию")
        form.addRow("Голос:", self._voice)

        self._wake_button = QLineEdit(self._cfg.wake.mouse_button)
        self._wake_button.setToolTip(
            "Кнопка мыши для активации: mouse4, mouse5, middle.\n"
            "Пусто — не использовать мышь.")
        form.addRow("Кнопка мыши:", self._wake_button)

        self._hotkey = QLineEdit(self._cfg.wake.hotkey)
        self._hotkey.setToolTip("Сочетание клавиш: ctrl+space, f9")
        form.addRow("Горячая клавиша:", self._hotkey)

        self._ptt = QLineEdit(self._cfg.wake.push_to_talk)
        self._ptt.setToolTip("Удержание — говори. Пусто — отключить.")
        form.addRow("Удержание (PTT):", self._ptt)

        tabs.addTab(self._wrap(box), "Голос")

    def _build_advanced_tab(self, tabs: QTabWidget, models, autostart_state) -> None:
        box = QGroupBox("Распознавание")
        form = QFormLayout(box)

        self._model = QComboBox()
        for name in (models or ["tiny", "base", "small", "medium",
                                "large-v3-turbo"]):
            self._model.addItem(name)
        self._model.setCurrentText(self._cfg.stt.model)
        self._model.setToolTip(
            "Больше модель — точнее распознавание и больше расход памяти.\n"
            "Требует перезапуска ассистента.")
        form.addRow("Модель:", self._model)

        self._min_conf = QLineEdit(str(self._cfg.stt.min_confidence))
        self._min_conf.setToolTip(
            "Ниже — фраза считается выдумкой и не становится командой.\n"
            "Если ассистент отвечает «не расслышал», снизьте.")
        form.addRow("Порог уверенности:", self._min_conf)

        self._keep_days = QLineEdit(str(self._cfg.privacy.keep_history_days))
        self._keep_days.setToolTip("Сколько дней хранить историю. 0 — не хранить.")
        form.addRow("Хранить историю, дней:", self._keep_days)

        extra = QVBoxLayout()

        self._autostart = QCheckBox("Запускать вместе с Windows")
        if autostart_state is not None:
            try:
                self._autostart.setChecked(bool(autostart_state()))
            except Exception as exc:
                log.warning("Настройки: не удалось прочитать автозапуск (%s)", exc)
        self._autostart.toggled.connect(self._toggle_autostart)
        extra.addWidget(self._autostart)

        warn = QLabel("Изменения модели и микрофона вступят в силу "
                      "после перезапуска ассистента.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #888;")
        extra.addWidget(warn)
        extra.addStretch(1)

        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.addWidget(box)
        outer.addLayout(extra)
        tabs.addTab(tab, "Распознавание")

    @staticmethod
    def _wrap(widget: QWidget) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.addWidget(widget)
        layout.addStretch(1)
        return holder

    # --- действия ----------------------------------------------------------

    def _preview_mic(self) -> None:
        if self._on_mic_changed is not None:
            self._on_mic_changed(self._mic.currentData())

    def _toggle_autostart(self, checked: bool) -> None:
        if self._on_autostart is None:
            return
        problem = ""
        ok = False
        try:
            ok = self._on_autostart(checked)
        except Exception as exc:                    # noqa: BLE001
            log.warning("Настройки: автозапуск не изменился (%s)", exc)
            problem = f"Не удалось изменить автозапуск:\n{exc}"

        # `on_autostart` сообщает неудачу и возвратом, и исключением —
        # оба случая обязаны откатить галочку. Иначе интерфейс покажет
        # включённый автозапуск, которого нет.
        if not ok:
            self._autostart.blockSignals(True)
            self._autostart.setChecked(not checked)
            self._autostart.blockSignals(False)
            QMessageBox.warning(self, "Автозапуск",
                                problem or "Не удалось изменить автозапуск.")

    def _save(self) -> None:
        cfg = self._cfg

        cfg.audio.input_device = self._mic.currentData()

        try:
            cfg.vad.silence_ms = max(100, int(float(self._quiescence.text())))
        except ValueError:
            QMessageBox.warning(self, "Настройки",
                                "Поле «Конец фразы» должно быть числом.")
            return

        try:
            cfg.wake.listen_timeout_s = max(1.0, float(self._timeout.text()))
        except ValueError:
            QMessageBox.warning(self, "Настройки",
                                "Поле «Окно ожидания речи» должно быть числом.")
            return

        try:
            cfg.stt.min_confidence = float(self._min_conf.text())
        except ValueError:
            QMessageBox.warning(self, "Настройки",
                                "Порог уверенности должен быть числом.")
            return

        try:
            cfg.privacy.keep_history_days = max(0, int(
                float(self._keep_days.text())))
        except ValueError:
            QMessageBox.warning(self, "Настройки",
                                "Срок хранения истории должен быть числом.")
            return

        cfg.tts.voice = self._voice.currentText().strip()
        cfg.wake.mouse_button = self._wake_button.text().strip()
        cfg.wake.hotkey = self._hotkey.text().strip()
        cfg.wake.push_to_talk = self._ptt.text().strip()
        cfg.stt.model = self._model.currentText().strip()

        # Сохраняем сами, а не через обратный вызов. Иначе потеря настроек
        # зависит от того, передал ли хост `on_apply`: молча пропавшие
        # галочки — худший вид поломки, потому что он заметен только
        # через месяц, когда ассистент ведёт себя не так.
        try:
            save_config(cfg)
        except OSError as exc:
            log.warning("Настройки: не удалось записать config.yaml (%s)", exc)
            QMessageBox.warning(
                self, "Настройки",
                f"Не удалось сохранить настройки:\n{exc}")
            return

        if self._on_apply is not None:
            try:
                self._on_apply({"speak_enabled": self._speaking.isChecked()})
            except Exception:
                log.exception("Настройки: сбой применения")

        self.close()