"""Контракт движка распознавания речи.

Единый интерфейс для всех STT, чтобы пайплайн не знал, что именно стоит
под ним. Все реализации возвращают `Transcript` и принимают float32 mono
16 кГц — формат, который понимает любая модель.

Что важно в контракте:
  * `transcribe()` — блокирующая (CPU/GPU-bound) функция. Её обязательно
    вызывать в executor, иначе она встанет в event loop;
  * `confidence` — не «точность в процентах», а эвристика, по которой
    решаем, стоит ли переспрашивать пользователя;
  * `warmup()` — прогрев модели. Первый запрос по cold-модели занимает
    в разы больше, поэтому важен для задержки.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass

import numpy as np

from ..audio.capture import dbfs, normalize_peak, trim_silence
from ..core.errors import EngineUnavailable

log = logging.getLogger(__name__)


@dataclass
class Transcript:
    """Результат распознавания."""

    text: str
    confidence: float = 1.0
    is_final: bool = True
    language: str = "ru"
    engine: str = ""
    elapsed: float = 0.0  # сколько заняло распознавание, секунды

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def __str__(self) -> str:
        return f"[{self.engine}] {self.text!r} conf={self.confidence:.2f} {self.elapsed:.2f}s"


class STTEngine(abc.ABC):
    """Базовый класс движка распознавания."""

    name: str = "base"

    def __init__(
        self,
        *,
        language: str = "ru",
        normalize_audio: bool = True,
        min_dbfs: float = -50.0,
    ) -> None:
        self.language = language
        self.normalize_audio = normalize_audio
        self.min_dbfs = min_dbfs
        self._warmed = False

    # --- обязательное ------------------------------------------------------

    @abc.abstractmethod
    def _load(self) -> None:
        """Загрузить модель. Бросает `EngineUnavailable`, если не вышло."""

    @abc.abstractmethod
    def _recognize(self, audio: np.ndarray) -> Transcript:
        """Синхронное распознавание. Вызывается внутри executor'а."""

    # --- публичное ---------------------------------------------------------

    def warmup(self) -> None:
        """Прогрев: один короткий запрос, чтобы модель «проснулась»."""
        if self._warmed:
            return
        started = time.perf_counter()
        self._recognize(np.zeros(self.sample_rate // 2, dtype=np.float32))
        self._warmed = True
        log.info("%s прогрет за %.2f с", self.name, time.perf_counter() - started)

    def transcribe_sync(self, audio: np.ndarray) -> Transcript:
        """Синхронный вызов (для тестов и офлайн-обработки файлов)."""
        audio = self._prepare(audio)
        started = time.perf_counter()
        result = self._recognize(audio)
        result.elapsed = time.perf_counter() - started
        result.engine = result.engine or self.name
        return result

    async def transcribe(self, audio: np.ndarray) -> Transcript:
        """Асинхронная обёртка: тяжёлое вычисление — в executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.transcribe_sync, audio)

    # --- внутреннее --------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return 16000

    def _prepare(self, audio: np.ndarray) -> np.ndarray:
        if audio is None or audio.size == 0:
            return np.zeros(0, dtype=np.float32)

        # Барьер по громкости — до модели. Тишина и шум не должны попадать
        # в Whisper: там они превращаются в выдуманные фразы («субтитры»,
        # «спокойная музыка»), а распознавание всё равно занимает ~0.5 с.
        if self.min_dbfs > -120.0 and dbfs(audio) < self.min_dbfs:
            log.info("Фраза тише %.0f dBFS (порог %.0f) — в модель не иду",
                     dbfs(audio), self.min_dbfs)
            return np.zeros(0, dtype=np.float32)

        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if self.normalize_audio:
            # Нормализация громкости и обрезка тишины по краям заметно
            # улучшают точность на тихих микрофонах.
            before = audio.size
            audio = normalize_peak(audio)
            audio = trim_silence(audio, sample_rate=self.sample_rate)
            # Сколько реально дошло до модели. Без этого числа любая
            # потеря качества выглядит загадкой: в логе видно только
            # итоговое «Processing audio with duration», из которого
            # непонятно, где именно audio похудел.
            log.debug("Подготовка аудио: %.2f с -> %.2f с (обрезано %.0f%%)",
                      before / self.sample_rate,
                      audio.size / self.sample_rate,
                      100.0 * (before - audio.size) / max(1, before))
        return audio

    @staticmethod
    def estimate_confidence(avg_logprob: float | None, no_speech_prob: float | None) -> float:
        """Эвристика уверенности из выходов Whisper.

        `avg_logprob` — средний логарифм вероятности на токен (ближе к 0 —
        увереннее); `no_speech_prob` — вероятность, что это вообще не речь.
        """
        conf = 1.0
        if avg_logprob is not None:
            conf *= float(np.exp(avg_logprob))
        if no_speech_prob is not None:
            conf *= float(max(0.0, 1.0 - no_speech_prob))
        return round(min(1.0, max(0.0, conf)), 3)


def load_engine(name: str, **kwargs) -> STTEngine:
    """Фабрика движков по имени из конфига."""
    name = (name or "").lower()

    if name in ("faster_whisper", "whisper"):
        from .faster_whisper import FasterWhisperEngine

        return FasterWhisperEngine(**kwargs)
    if name in ("vosk", "vosk_stream"):
        from .vosk_stream import VoskEngine

        return VoskEngine(**kwargs)
    if name in ("", "none", "off"):
        from .null_engine import NullSTT

        return NullSTT(**kwargs)

    raise EngineUnavailable(name, "Допустимо: faster_whisper, vosk, none")
