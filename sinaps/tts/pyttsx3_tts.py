"""Системный синтезатор pyttsx3 (SAPI5 в Windows, NSSpeech в macOS,
Festival/espeak в Linux).

Роль: **запасной вариант**. Если ни Silero, ни Piper не загрузились,
ассистент всё равно должен говорить — пусть роботом, но говорить.

Плюсы: ноль дополнительных моделей, есть на любой машине с Python.
Минусы: качество заметно ниже, на некоторых системах голоса по умолчанию
английские — поэтому отдельно ищем русский.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path
from tempfile import gettempdir

import numpy as np

from ..core.errors import EngineUnavailable
from .base import TTSEngine

log = logging.getLogger(__name__)

DEFAULT_RATE = 175  # слов в минуту


class Pyttsx3TTS(TTSEngine):
    name = "pyttsx3"

    def __init__(
        self,
        *,
        voice: str = "",
        speed: float = 1.0,
        volume: float = 1.0,
        **_ignored: object,
    ) -> None:
        super().__init__(volume=volume, speed=speed)
        self.voice_name = voice
        self._engine = None
        self._rate = int(DEFAULT_RATE * speed)
        self._sample_rate = 22050
        self._lock = threading.Lock()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def _load(self) -> None:
        with self._lock:
            if self._engine is not None:
                return
            try:
                import pyttsx3
            except ImportError as exc:
                raise EngineUnavailable(
                    self.name, "Установи: .venv\\Scripts\\pip install pyttsx3"
                ) from exc

            try:
                self._engine = pyttsx3.init()
            except Exception as exc:
                raise EngineUnavailable(
                    self.name, f"Системный синтезатор недоступен: {exc}"
                ) from exc

            self._pick_russian_voice()
            self._engine.setProperty("rate", self._rate)
            self._engine.setProperty("volume", self.volume)
            log.info("Системный синтезатор готов (голос: %s)", self._current_voice())

    def _voices(self) -> list:  # noqa: ANN202
        try:
            return list(self._engine.getProperty("voices") or [])
        except Exception:
            return []

    def _current_voice(self) -> str:
        try:
            return (self._engine.getProperty("voice") or "") or "(по умолчанию)"
        except Exception:
            return "?"

    def _pick_russian_voice(self) -> None:
        """Ищем русский голос: сначала по точному имени, потом по признакам."""
        voices = self._voices()
        if not voices:
            return

        def key(v) -> str:  # noqa: ANN001
            return ((v.id or "") + " " + (v.name or "")).lower()

        if self.voice_name:
            needle = self.voice_name.lower()
            for v in voices:
                if needle in key(v):
                    self._engine.setProperty("voice", v.id)
                    return
            log.warning("Голос '%s' не найден, беру первый доступный", self.voice_name)

        markers = ("ru", "russian", "русск", "russ", "yuri", "pavel")
        for marker in markers:
            for v in voices:
                if marker in key(v):
                    self._engine.setProperty("voice", v.id)
                    log.info("Выбран русский системный голос: %s", v.name)
                    return
        log.warning("Русский системный голос не найден — будет голос по умолчанию")

    def _synthesize(self, text: str) -> np.ndarray:
        self._load()

        # pyttsx3 умеет только `save_to_file` + `runAndWait` — приходится
        # писать во временный WAV. Это его главное ограничение.
        tmp = Path(gettempdir()) / "sinaps_tts_tmp.wav"
        with self._lock:
            self._engine.save_to_file(text, str(tmp))
            self._engine.runAndWait()

        if not tmp.exists():
            log.warning("Системный синтезатор не создал файл")
            return np.zeros(0, dtype=np.float32)

        with wave.open(str(tmp), "rb") as wf:
            rate = wf.getframerate()
            width = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        tmp.unlink(missing_ok=True)

        if width != 2:
            log.warning("Неожиданная ширина сэмпла %s в системном синтезаторе", width)
            return np.zeros(0, dtype=np.float32)

        self._sample_rate = rate
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
        return np.ascontiguousarray(data, dtype=np.float32)

    def set_speed(self, speed: float) -> None:
        super().set_speed(speed)
        self._rate = int(DEFAULT_RATE * self.speed)
        if self._engine is not None:
            self._engine.setProperty("rate", self._rate)
