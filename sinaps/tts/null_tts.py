"""TTS-заглушка: ничего не синтезирует, только печатает.

Нужна для тестов и режима `--dry-run`, где важна скорость и не нужен звук.
"""

from __future__ import annotations

import logging

import numpy as np

from .base import TTSEngine

log = logging.getLogger(__name__)


class NullTTS(TTSEngine):
    name = "none"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        #: Последний синтезированный текст — тесты проверяют его вместо звука.
        self.last_text: str = ""
        self.calls = 0

    def _synthesize(self, text: str) -> np.ndarray:
        self.last_text = text
        self.calls += 1
        return np.zeros(0, dtype=np.float32)

    async def speak(self, text: str, player) -> bool:  # type: ignore[override]
        self.last_text = (text or "").strip()
        self.calls += 1
        log.info("NullTTS: %s", self.last_text)
        return True
