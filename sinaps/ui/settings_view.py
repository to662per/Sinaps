# -*- coding: utf-8 -*-
"""Вкладка «Настройки»: то, что раньше правили в `config.yaml` руками.

Разделы те же, что в файле конфигурации, и в том же порядке: человек,
который открывал YAML, найдёт здесь свои настройки по старым именам.

Значения не применяются на лету. Модель распознавания и устройство
выбираются при старте, и менять их на ходу значило бы молча перезагружать
движок посреди разговора. Такие настройки помечены и подписаны: «с
перезапуска», чтобы человек знал, а не гадал.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from .theme import (CARD, DANGER, EDGE, MUTED, SUCCESS, TEXT, WARNING,
                    button, color, font, label, rgba, shell_style)

#: Настройки, которые нельзя применить без перезапуска.
RESTART_ONLY = frozenset({
    "stt.model", "stt.device", "stt.engine", "audio.input_device",
    "audio.output_device",
})

#: Префиксы настроек, меняющих движки. Проверяются по началу пути,
#: а не по точному совпадению: `stt.model_size` имеет право быть
#: настройкой с перезапуском так же, как `stt.model`.
RESTART_MARKERS = ("stt.", "audio.input_device", "audio.output_device")


class _Row(QWidget):
    """Строка настройки: подпись, поле и пояснение."""

    def __init__(self, path: str, caption: str, widget: QWidget,
                 hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self._hint = hint

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(10)

        text = QLabel(caption)
        text.setFont(font(9))
        text.setMinimumWidth(150)
        text.setStyleSheet(label(size=9))
        if path in RESTART_ONLY:
            # Пометка видна сразу, а не после того, как человек перезапустит
            # ассистент и не заметит перемены.
            text.setText(f"{caption}  • с перезапуска")
            text.setToolTip("Применится после перезапуска ассистента")
        layout.addWidget(text)

        layout.addWidget(widget, 1)

        if hint:
            note = QLabel(hint)
            note.setFont(font(8))
            note.setStyleSheet(label(size=8, tone=MUTED))
            layout.addWidget(note, 1)

    @property
    def hint(self) -> str:
        return self._hint


class _DeviceCombo(QComboBox):
    """Список устройств: наружу уходит индекс, а не имя.

    Имя устройства меняется при обновлении Windows — «Микрофон (Realtek
    Audio)» становится «Микрофон (Realtek(R) Audio)», и сохранённое имя
    перестаёт находиться. Индекс Portaudio переживает обновление, а
    конфигурация уже умеет принимать и его, и имя: `resolve_device`
    разбирает оба случая. Поэтому наружу уходит то, что переживёт
    перезагрузку, иначе настройка тихо перестанет работать.
    """


class SettingsView(QWidget):
    """Настройки, сгруппированные по разделам конфигурации."""

    #: Изменение настройки: (путь, значение).
    changed = Signal(str, object)

    def __init__(self, config=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[str, _Row] = {}
        self._config = config
        self._build()

    def set_devices(self, inputs=None, outputs=None) -> None:
        """Наполнить списки микрофонов и динамиков.

        Список берётся у `sinaps.audio.devices` — того же места, что и
        для консоли. Свой обход Portaudio означал бы, что окно и
        командная строка видят разные микрофоны, и «в консоли слышно,
        а в окне нет» объяснить будет нечем.

        Отсутствие устройств — не ошибка: ассистент работает с клавиатуры,
        и пустой список здесь честнее выдуманного.
        """
        for path, devices in (("audio.input_device", inputs),
                              ("audio.output_device", outputs)):
            row = self._rows.get(path)
            if row is None:
                continue
            for control in _controls(row):
                if not isinstance(control, QComboBox):
                    continue
                control.blockSignals(True)
                control.clear()
                control.addItem("Системный по умолчанию", None)
                for item in (devices or ()):
                    if isinstance(item, (tuple, list)):
                        index, name = item[0], item[1]
                    else:
                        index, name = getattr(item, "index", None), \
                            getattr(item, "name", str(item))
                    control.addItem(str(name), index)
                control.blockSignals(False)

    # --- построение -------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        # Прокрутка нужна уже сейчас: разделов семь, а окно не резиновое.
        from PySide6.QtWidgets import QScrollArea

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 8, 0)
        column.setSpacing(14)

        column.addWidget(self._section("Звук", self._audio_rows()))
        column.addWidget(self._section("Голос", self._voice_rows()))
        column.addWidget(self._understanding_rows_section())
        column.addWidget(self._section("Активация", self._activation_rows()))
        column.addWidget(self._section("Приватность", self._privacy_rows()))
        column.addWidget(self._plugins_note())
        column.addStretch(1)

        scroll.setWidget(content)

    def _section(self, title: str, rows) -> QWidget:
        box = QFrame()
        box.setObjectName(f"set_{title.lower()}")
        box.setStyleSheet(f"""
            #{box.objectName()} {{
                background: {rgba(CARD, 255)};
                border: 1px solid {rgba(EDGE, 160)};
                border-radius: 10px;
            }}
        """)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        caption = QLabel(title)
        caption.setFont(font(10, bold=True))
        caption.setStyleSheet(label(size=10, bold=True))
        layout.addWidget(caption)

        for row in rows:
            layout.addWidget(row)
        return box

    def _understanding_rows_section(self) -> QWidget:
        """«Понимание» — отдельно из-за обилия полей."""
        rows = []

        model = QComboBox()
        model.addItems(["tiny", "base", "small", "medium",
                        "large-v3-turbo"])
        model.setStyleSheet(self._combo_style())
        rows.append(_Row("stt.model", "Модель распознавания", model,
                         "tiny — черновик, large-v3-turbo — лучшее качество"))

        device = QComboBox()
        device.addItems(["auto", "cuda", "cpu"])
        device.setStyleSheet(self._combo_style())
        rows.append(_Row("stt.device", "Устройство", device,
                         "auto — видеокарта, если есть"))

        confidence = QDoubleSpinBox()
        confidence.setRange(0.0, 1.0)
        confidence.setSingleStep(0.05)
        confidence.setDecimals(2)
        confidence.setStyleSheet(self._spin_style())
        rows.append(_Row("stt.min_confidence", "Порог уверенности",
                         confidence,
                         "ниже — считаем галлюцинацией, не команду"))

        threshold = QDoubleSpinBox()
        threshold.setRange(0.05, 0.95)
        threshold.setSingleStep(0.05)
        threshold.setDecimals(2)
        threshold.setStyleSheet(self._spin_style())
        rows.append(_Row("vad.threshold", "Чувствительность микрофона",
                         threshold, "меньше — тише слышно"))

        silence = QSpinBox()
        silence.setRange(200, 3000)
        silence.setSingleStep(100)
        silence.setSuffix(" мс")
        silence.setStyleSheet(self._spin_style())
        rows.append(_Row("vad.silence_ms", "Пауза = конец фразы", silence))

        vad_engine = QComboBox()
        vad_engine.addItems(["silero", "energy"])
        vad_engine.setStyleSheet(self._combo_style())
        rows.append(_Row("vad.engine", "Детектор речи", vad_engine,
                         "energy — запасной, без моделей"))

        min_speech = QSpinBox()
        min_speech.setRange(50, 2000)
        min_speech.setSingleStep(50)
        min_speech.setSuffix(" мс")
        min_speech.setStyleSheet(self._spin_style())
        rows.append(_Row("vad.min_speech_ms", "Короче — считаем шумом",
                         min_speech))

        max_utterance = QDoubleSpinBox()
        max_utterance.setRange(1.0, 120.0)
        max_utterance.setSingleStep(1.0)
        max_utterance.setDecimals(0)
        max_utterance.setSuffix(" с")
        max_utterance.setStyleSheet(self._spin_style())
        rows.append(_Row("vad.max_utterance_s", "Длиннее фразы не бывает",
                         max_utterance, "страховка от бесконечной записи"))

        stt_engine = QComboBox()
        stt_engine.addItems(["faster_whisper", "vosk", "none"])
        stt_engine.setStyleSheet(self._combo_style())
        rows.append(_Row("stt.engine", "Движок распознавания", stt_engine))

        compute = QComboBox()
        compute.addItems(["auto", "int8", "float16", "int8_float16"])
        compute.setStyleSheet(self._combo_style())
        rows.append(_Row("stt.compute_type", "Точность вычислений", compute))

        language = QComboBox()
        language.setEditable(True)
        language.setStyleSheet(self._combo_style())
        language.setCurrentText("ru")
        rows.append(_Row("stt.language", "Язык", language))

        beam = QSpinBox()
        beam.setRange(1, 10)
        beam.setStyleSheet(self._spin_style())
        rows.append(_Row("stt.beam_size", "Луч поиска", beam,
                         "1 — быстрее, больше — точнее и медленнее"))

        fallback = QDoubleSpinBox()
        fallback.setRange(0.0, 1.0)
        fallback.setSingleStep(0.05)
        fallback.setDecimals(2)
        fallback.setStyleSheet(self._spin_style())
        rows.append(_Row("stt.fallback_confidence", "Порог запасного движка",
                         fallback, "ниже порога пробуем другой движок"))

        prompt = QComboBox()
        prompt.setEditable(True)
        prompt.setStyleSheet(self._combo_style())
        prompt.setPlaceholderText("словарь для Whisper")
        rows.append(_Row("stt.initial_prompt", "Подсказка слов", prompt,
                         "часто слова, произносимые неверно"))

        dbfs = QDoubleSpinBox()
        dbfs.setRange(-120.0, 0.0)
        dbfs.setSingleStep(5.0)
        dbfs.setDecimals(0)
        dbfs.setSuffix(" дБ")
        dbfs.setStyleSheet(self._spin_style())
        rows.append(_Row("stt.min_dbfs", "Порог громкости", dbfs,
                         "ниже — в модель не идём"))

        self._register(rows)
        return self._section("Понимание", rows)

    def _audio_rows(self) -> list:
        rows = []

        device = _DeviceCombo()
        device.setStyleSheet(self._combo_style())
        rows.append(_Row("audio.input_device", "Микрофон ввода", device,
                         "первый в списке — системный по умолчанию"))

        output = _DeviceCombo()
        output.setStyleSheet(self._combo_style())
        rows.append(_Row("audio.output_device", "Звук ответа", output,
                         "динамики, на которые говорит ассистент"))

        pre_roll = QSpinBox()
        pre_roll.setRange(0, 2000)
        pre_roll.setSingleStep(100)
        pre_roll.setSuffix(" мс")
        pre_roll.setStyleSheet(self._spin_style())
        rows.append(_Row("audio.pre_roll_ms", "Запись до начала речи",
                         pre_roll,
                         "больше — не теряется первое слово"))

        sample_rate = QComboBox()
        sample_rate.addItems(["16000"])
        sample_rate.setStyleSheet(self._combo_style())
        rows.append(_Row("audio.sample_rate", "Частота записи", sample_rate,
                         "требование всех движков распознавания"))

        frame_ms = QSpinBox()
        frame_ms.setRange(5, 100)
        frame_ms.setSingleStep(5)
        frame_ms.setSuffix(" мс")
        frame_ms.setStyleSheet(self._spin_style())
        rows.append(_Row("audio.frame_ms", "Размер фрейма", frame_ms,
                         "размер порции от Portaudio"))

        latency = QComboBox()
        latency.addItems(["low", "high"])
        latency.setStyleSheet(self._combo_style())
        rows.append(_Row("audio.latency", "Буфер захвата", latency,
                         "high нужен, если звук рвётся"))

        normalize = QCheckBox("выравнивать громкость")
        normalize.setFont(font(9))
        normalize.setStyleSheet(label(size=9))
        rows.append(_Row("audio.peak_normalize", "Нормализация", normalize,
                         "привести пики к одному уровню"))

        self._register(rows)
        return rows

    def _voice_rows(self) -> list:
        rows = []

        voice = QComboBox()
        voice.setStyleSheet(self._combo_style())
        voice.setEditable(True)
        voice.setPlaceholderText("Голос по умолчанию")
        rows.append(_Row("tts.voice", "Голос", voice))

        speed = QDoubleSpinBox()
        speed.setRange(0.5, 2.0)
        speed.setSingleStep(0.1)
        speed.setDecimals(1)
        speed.setStyleSheet(self._spin_style())
        rows.append(_Row("tts.speed", "Скорость", speed,
                         "1.0 — обычная, больше — быстрее"))

        test = QPushButton("Сказать пример")
        test.setCursor(Qt.CursorShape.PointingHandCursor)
        test.setStyleSheet(button(
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 160)",
            f"rgba({EDGE.red()}, {EDGE.green()}, {EDGE.blue()}, 255)"))
        test.clicked.connect(lambda: self.changed.emit("tts.test", ""))
        rows.append(_Row("tts.test", "Проверка голоса", test,
                         "произносит фразу с текущими настройками"))

        engine = QComboBox()
        engine.addItems(["silero", "piper", "none"])
        engine.setStyleSheet(self._combo_style())
        rows.append(_Row("tts.engine", "Движок", engine))

        volume = QDoubleSpinBox()
        volume.setRange(0.0, 1.0)
        volume.setSingleStep(0.1)
        volume.setDecimals(1)
        volume.setStyleSheet(self._spin_style())
        rows.append(_Row("tts.volume", "Громкость ответа", volume,
                         "0 — молча, 1 — на максимум"))

        sample_rate = QComboBox()
        sample_rate.addItems(["24000"])
        sample_rate.setStyleSheet(self._combo_style())
        rows.append(_Row("tts.sample_rate", "Частота синтеза", sample_rate,
                         "Silero v5 выдаёт 24 кГц"))

        greet = QCheckBox("здороваться при старте")
        greet.setFont(font(9))
        greet.setStyleSheet(label(size=9))
        rows.append(_Row("tts.greet_on_start", "Приветствие", greet,
                         "полезно с автозапуском, утомительно при каждой "
                         "перезагрузке"))

        self._register(rows)
        return rows

    def _activation_rows(self) -> list:
        rows = []

        hotkey = QComboBox()
        hotkey.setEditable(True)
        hotkey.setStyleSheet(self._combo_style())
        hotkey.setCurrentText("ctrl+space")
        rows.append(_Row("wake.hotkey", "Горячая клавиша", hotkey,
                         "запасной способ с клавиатуры"))

        mouse = QComboBox()
        mouse.setEditable(True)
        mouse.setStyleSheet(self._combo_style())
        mouse.setCurrentText("mouse5")
        rows.append(_Row("wake.mouse_button", "Кнопка мыши", mouse,
                         "открыть окно прослушивания"))

        ptt = QComboBox()
        ptt.setEditable(True)
        ptt.setStyleSheet(self._combo_style())
        ptt.setCurrentText("mouse4")
        rows.append(_Row("wake.push_to_talk", "Говори, пока держишь", ptt))

        timeout = QDoubleSpinBox()
        timeout.setRange(1.0, 60.0)
        timeout.setSingleStep(1.0)
        timeout.setDecimals(0)
        timeout.setSuffix(" с")
        timeout.setStyleSheet(self._spin_style())
        rows.append(_Row("wake.listen_timeout_s", "Окно ожидания фразы",
                         timeout))

        mode = QComboBox()
        mode.addItems(["hotkey", "none"])
        mode.setStyleSheet(self._combo_style())
        rows.append(_Row("wake.mode", "Режим активации", mode,
                         "hotkey — кнопкой; none — только вручную"))

        self._register(rows)
        return rows

    def _privacy_rows(self) -> list:
        rows = []

        keep = QSpinBox()
        keep.setRange(0, 3650)
        keep.setSingleStep(1)
        keep.setSuffix(" дн.")
        keep.setStyleSheet(self._spin_style())
        rows.append(_Row("privacy.keep_history_days", "Хранить историю",
                         keep, "0 — не хранить вовсе"))

        record = QCheckBox("записывать микрофон на диск")
        record.setFont(font(9))
        record.setStyleSheet(label(size=9))
        rows.append(_Row("privacy.record_audio", "Запись аудио", record,
                         "только для отладки"))

        note = QLabel(
            "Настройки сети нет и не будет: ядро не ходит в интернет "
            "вообще. Права плагинов — в разделе «Плагины».")
        note.setFont(font(8))
        note.setWordWrap(True)
        note.setStyleSheet(label(size=8, tone=MUTED))
        rows.append(_Row("privacy.note", "Сеть", note,
                         "запрет сделан структурой кода, а не флажком"))

        self._register(rows)
        return rows

    def _plugins_note(self) -> QWidget:
        """Указание на раздел «Плагины».

        Отдельного раздела настроек плагинов нет: права и включение
        живут в карточках плагинов, где видно, что именно одобрено.
        Дублировать список тут — значит держать два места, которые
        разойдутся.
        """
        box = QFrame()
        box.setObjectName("set_plugins_note")
        box.setStyleSheet(f"""
            #set_plugins_note {{
                background: {rgba(CARD, 255)};
                border: 1px solid {rgba(EDGE, 160)};
                border-radius: 10px;
            }}
        """)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        caption = QLabel("Плагины")
        caption.setFont(font(10, bold=True))
        caption.setStyleSheet(label(size=10, bold=True))
        layout.addWidget(caption)

        note = QLabel(
            "Права, одобрение и включение — в разделе «Плагины». "
            "Отзыв права и выключение спрашивают подтверждения голосом.")
        note.setFont(font(9))
        note.setWordWrap(True)
        note.setStyleSheet(label(size=9, tone=MUTED))
        layout.addWidget(note)

        return box

    # --- значения ---------------------------------------------------------

    def _register(self, rows) -> None:
        """Запомнить строки и подписать их на изменение."""
        for row in rows:
            self._rows[row.path] = row
            for control in _controls(row):
                if isinstance(control, QCheckBox):
                    control.toggled.connect(
                        lambda checked, p=row.path: self.changed.emit(p, checked))
                elif isinstance(control, _DeviceCombo):
                    # Устройство отдаёт индекс, а не подпись: имя
                    # меняется вместе с Windows.
                    control.currentIndexChanged.connect(
                        lambda _i, c=control, p=row.path:
                        self.changed.emit(p, c.currentData()))
                elif isinstance(control, QComboBox):
                    control.currentTextChanged.connect(
                        lambda text, p=row.path: self.changed.emit(p, text))
                else:
                    control.valueChanged.connect(
                        lambda value, p=row.path: self.changed.emit(p, value))

    def load(self, config) -> None:
        """Показать значения из конфигурации.

        Отсутствующее значение оставляем как есть, а не ставим ноль:
        тихий ноль в поле «модель распознавания» выглядит как выбор,
        и человек потом гадает, почему ассистент не понимает речь.
        """
        self._config = config
        for path, row in self._rows.items():
            value = _lookup(config, path)
            if value is None:
                continue
            for control in _controls(row):
                _apply(control, value)

    @staticmethod
    def _combo_style() -> str:
        return f"""
            QComboBox {{
                background: {rgba(CARD, 255)};
                border: 1px solid {rgba(EDGE, 200)};
                border-radius: 8px;
                padding: 5px 8px;
                color: {color(TEXT)};
                min-width: 130px;
            }}
        """

    @staticmethod
    def _spin_style() -> str:
        return f"""
            QSpinBox, QDoubleSpinBox {{
                background: {rgba(CARD, 255)};
                border: 1px solid {rgba(EDGE, 200)};
                border-radius: 8px;
                padding: 5px 8px;
                color: {color(TEXT)};
                max-width: 140px;
            }}
        """


#: Типы полей, в которые человек вводит значение.
_FIELD_TYPES = (QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox)


def _controls(row: QWidget) -> list:
    """Поля строки настройки.

    `findChildren` в PySide6 принимает один тип, а не кортеж, поэтому
    типы опрашиваются по очереди. Порядок не важен: каждая строка
    содержит ровно одно поле.
    """
    found: list = []
    for kind in _FIELD_TYPES:
        found.extend(row.findChildren(kind))
    return found


def _lookup(config, path: str):
    """Значение настройки по пути `stt.model` → `config.stt.model`."""
    if config is None:
        return None
    current = config
    for part in path.split("."):
        if current is None:
            return None
        current = getattr(current, part, None)
    return current


def _apply(control, value) -> None:
    """Записать значение в поле, не породив сигнал изменения."""
    if isinstance(control, QCheckBox):
        control.blockSignals(True)
        control.setChecked(bool(value))
        control.blockSignals(False)
    elif isinstance(control, _DeviceCombo):
        # Настройка может хранить и индекс, и имя — `resolve_device`
        # принимает оба. Показываем то, чему значение равно на самом
        # деле, а не всегда первый подходящий: иначе микрофон, выбранный
        # по имени, тихо заменялся бы системным.
        control.blockSignals(True)
        index = control.findData(value)
        if index < 0:
            index = control.findText(str(value))
        if index >= 0:
            control.setCurrentIndex(index)
        control.blockSignals(False)
    elif isinstance(control, QComboBox):
        control.blockSignals(True)
        text = str(value)
        if control.isEditable():
            control.setCurrentText(text)
        else:
            index = control.findText(text)
            if index >= 0:
                control.setCurrentIndex(index)
        control.blockSignals(False)
    elif isinstance(control, QSpinBox):
        control.blockSignals(True)
        control.setValue(int(value))
        control.blockSignals(False)
    elif isinstance(control, QDoubleSpinBox):
        control.blockSignals(True)
        control.setValue(float(value))
        control.blockSignals(False)


__all__ = ["RESTART_MARKERS", "RESTART_ONLY", "SettingsView"]