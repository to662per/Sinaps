"""Piper TTS — локальный синтезатор с более естественным звучанием.

Когда выбирать Piper вместо Silero:
  * качество важнее скорости разработки — Piper заметно «человечнее»;
  * Silero не справился с интонацией (усталый/эмоциональный текст).

Цена: голоса надо скачать отдельно (~60 МБ на голос) и положить в models/piper.
Русские голоса: `ru_RU-denis-medium`, `ru_RU-dmitri-medium` (см. huggingface.co/rhasspy/piper-voices).

Модель работает на CPU через onnxruntime — видеокарта не нужна.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np

from ..core.errors import EngineUnavailable
from .base import TTSEngine

log = logging.getLogger(__name__)

DEFAULT_VOICE_PATH = "models/piper/ru_RU-denis-medium.onnx"


class PiperTTS(TTSEngine):
    name = "piper"

    def __init__(
        self,
        *,
        voice: str = "",
        speed: float = 1.0,
        volume: float = 1.0,
        model_path: str | None = None,
        **_ignored: object,
    ) -> None:
        super().__init__(volume=volume, speed=speed)
        self.model_path = model_path or (voice or DEFAULT_VOICE_PATH)
        self._voice = None
        self._sample_rate = 22050

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def _load(self) -> None:
        if self._voice is not None:
            return

        path = Path(self.model_path)
        if not path.exists():
            raise EngineUnavailable(
                self.name,
                f"Модель Piper не найдена: {path}. Скачай русский голос с "
                "https://huggingface.co/rhasspy/piper-voices и укажи путь в config.yaml",
            )

        try:
            from piper.voice import PiperVoice
        except ImportError:
            try:
                from piper_phonemize import PiperVoice  # type: ignore
            except ImportError as exc:
                raise EngineUnavailable(
                    self.name, "Установи: .venv\\Scripts\\pip install piper-tts"
                ) from exc

        try:
            self._voice = PiperVoice.load(str(path))
            self._sample_rate = self._voice.config.sample_rate
            log.info("Piper загружен: %s (%d Гц)", path.name, self._sample_rate)
        except Exception as exc:
            raise EngineUnavailable(self.name, str(exc)) from exc

    def _synthesize(self, text: str) -> np.ndarray:
        self._load()

        # Piper умеет синтезировать прямо в WAV-поток — так быстрее, чем
        # собирать WAV в память целиком.
        with wave.open(str(Path(self.model_path).with_suffix(".tmp.wav")), "wb") as wav:
            self._voice.synthesize(text, wav, length_scale=1.0 / self.speed)

        data, rate = _read_wave(Path(self.model_path).with_suffix(".tmp.wav"))
        Path(self.model_path).with_suffix(".tmp.wav").unlink(missing_ok=True)

        if rate != self._sample_rate:
            self._sample_rate = rate
        return data


def _read_wave(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    if width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483647.0
    else:
        raise EngineUnavailable("piper", f"Неподдерживаемая ширина сэмпла: {width}")
    return data, rate
