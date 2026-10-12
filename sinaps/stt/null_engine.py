"""STT-заглушка.

Нужна, чтобы пайплайн работал и тестировался без микрофона, моделей и
тяжёлых зависимостей: принимает реплику из очереди (например, из
текстовой консоли) и возвращает её как «распознанную».
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from ..core.errors import TranscriptEmpty
from .base import STTEngine, Transcript

log = logging.getLogger(__name__)


class NullSTT(STTEngine):
    """Ничего не распознаёт: удобно для проверки контура без микрофона."""

    name = "none"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def _load(self) -> None:
        return None

    def _recognize(self, audio: np.ndarray) -> Transcript:
        return Transcript(text="", confidence=0.0, engine=self.name)

    async def push_text(self, text: str) -> None:
        """Кладёт текст в очередь, который потом вернёт следующий `transcribe`."""
        await self._queue.put(text)

    async def transcribe(self, audio: np.ndarray) -> Transcript:  # type: ignore[override]
        try:
            text = await asyncio.wait_for(self._queue.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return await super().transcribe(audio)
        if not text.strip():
            raise TranscriptEmpty()
        return Transcript(text=text, confidence=1.0, engine=self.name)
