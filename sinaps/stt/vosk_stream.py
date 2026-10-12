"""STT на Vosk — быстрый лёгкий движок для слабых машин и офлайн-KWS.

Когда нужен Vosk, а не Whisper:
  * CPU без GPU: модель small-ru весит ~50 МБ и работает быстрее real-time
    даже на слабом процессоре, тогда как Whisper на CPU считает секунды;
  * нужен KWS: у Vosk есть ограниченная грамматика — можно распознавать
    одно-два слова вместо всей фразы (будущий wake-word «Синапс»);
  * нужен офлайн на Raspberry Pi / старом ноутбуке.

Минусы: точность на русском заметно ниже Whisper, особенно на командах
с именами файлов и приложений.

Установка: `pip install vosk` + скачать модель с
https://alphacephei.com/vosk/models и указать путь в `config.yaml`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from ..core.errors import EngineUnavailable
from .base import STTEngine, Transcript

log = logging.getLogger(__name__)


class VoskEngine(STTEngine):
    name = "vosk"

    def __init__(
        self,
        *,
        model_path: str = "",
        language: str = "ru",
        sample_rate: int = 16000,
        words: bool = False,
        normalize_audio: bool = True,
        min_dbfs: float = -50.0,
    ) -> None:
        super().__init__(language=language, normalize_audio=normalize_audio,
                         min_dbfs=min_dbfs)
        self.model_path = model_path
        self._sample_rate = sample_rate
        self.words = words
        self._model = None
        self._recognizer = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def _load(self) -> None:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel

            SetLogLevel(-1)  # Vosk очень шумит в лог
        except ImportError as exc:
            raise EngineUnavailable(
                self.name, "Установи: .venv\\Scripts\\pip install vosk"
            ) from exc

        path = Path(self.model_path)
        if not path or not path.exists():
            raise EngineUnavailable(
                self.name,
                "Модель Vosk не найдена. Скачай с https://alphacephei.com/vosk/models "
                "и укажи путь в config.yaml -> stt.vosk_model_path",
            )

        self._model = Model(str(path))
        self._recognizer = KaldiRecognizer(self._model, self._sample_rate)
        self._recognizer.SetWords(self.words)
        log.info("Vosk загружен: %s", path)

    def _new_recognizer(self):  # noqa: ANN202
        from vosk import KaldiRecognizer

        rec = KaldiRecognizer(self._model, self._sample_rate)
        rec.SetWords(self.words)
        return rec

    def _recognize(self, audio: np.ndarray) -> Transcript:
        if self._model is None:
            self._load()

        if audio.size == 0:
            return Transcript(text="", confidence=0.0, engine=self.name)

        # Новый распознаватель на каждую фразу: фразы независимы, иначе
        # Vosk «докладывает» слова из предыдущей.
        rec = self._new_recognizer()
        rec.AcceptWaveform((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())

        # Частичные результаты пока не распознаны — дослушиваем до FinalResult.
        payload = json.loads(rec.FinalResult() or "{}")
        text = (payload.get("text") or "").strip().lower()

        # У Vosk нет собственной оценки уверенности; оцениваем по объёму
        # результата и наличию альтернатив в сыром выводе.
        alt_count = len(payload.get("alternatives", []))
        confidence = 1.0 if text and alt_count == 0 else (0.6 if text else 0.0)

        log.debug("Vosk распознал: %r", text)
        return Transcript(text=text, confidence=confidence, engine=self.name)

    # --- частичное распознавание ------------------------------------------

    def partial(self, audio: np.ndarray) -> str:
        """Промежуточный результат — для «живого» текста на экране."""
        if self._model is None:
            self._load()
        rec = self._new_recognizer()
        rec.AcceptWaveform((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        data = json.loads(rec.PartialResult() or "{}")
        return (data.get("partial") or "").strip().lower()
