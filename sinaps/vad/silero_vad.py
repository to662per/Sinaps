"""Определение границ речи (VAD) и шумоподавление.

VAD решает главную проблему голосового ввода: когда фраза закончилась?
Без него приходится ждать фиксированные 5 секунд (как в старом прототипе) —
это и медленно, и обрывает слова.

Два движка:
  * `silero` — нейросетевой, ~1 мс на фрейм, отличает речь от шума вентилятора;
  * `energy`  — запасной вариант по громкости, работает без моделей. Используется
    в тестах и если silero не установлен.

`SpeechSegmenter` — конечный автомат поверх движка. Он же отвечает за
«пользователь заговорил» — событие, на котором строится barge-in.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import AsyncIterator

import numpy as np

from ..audio.capture import SAMPLE_RATE, dbfs, rms

log = logging.getLogger(__name__)


class VadEngine:
    """Интерфейс детектора. Реализации: SileroVad, EnergyVad."""

    name = "base"

    def reset(self) -> None:
        """Сбросить состояние между фразами (у энергетического — базовый уровень)."""

    def probability(self, frame: np.ndarray) -> float:
        """Вероятность, что в кадре есть речь: 0.0 … 1.0."""
        raise NotImplementedError


class EnergyVad(VadEngine):
    """Порог по среднеквадратичной громкости относительно фонового шума.

    Алгоритм: измеряем «пол» шума в тишине, речь — всё, что выше `threshold × пол`.
    Простой, мгновенный, без моделей. Плохо работает, если шум постоянный.
    """

    name = "energy"

    def __init__(self, *, threshold: float = 2.8, noise_floor: float = 0.006,
                 calibration_frames: int = 12) -> None:
        self.threshold = threshold
        self.noise_floor = noise_floor
        self._calibration_left = calibration_frames
        self._accum = 0.0

    def reset(self) -> None:
        self._calibration_left = 8  # короткая перекалибровка после фразы

    def probability(self, frame: np.ndarray) -> float:
        level = rms(frame)
        if self._calibration_left > 0:
            self._calibration_left -= 1
            # Усредняем громкость первых кадров — это и есть фон.
            self.noise_floor = max(1e-4, 0.7 * self.noise_floor + 0.3 * level)
            return 0.0

        cutoff = max(self.noise_floor * self.threshold, 0.008)
        # Мягкая шкала: отношение уровня к порогу, ограниченное сверху.
        return float(np.clip(level / cutoff, 0.0, 1.0))


class SileroVad(VadEngine):
    """Нейросетевой VAD Silero через onnxruntime (CPU).

    Важная деталь: модель принимает **ровно 512 сэмплов** при 16 кГц (32 мс).
    Наши кадры по 20 мс (320 сэмплов) не подходят, поэтому внутри есть
    накопительный буфер: склеиваем кадры до 512 и считаем вероятность.
    """

    name = "silero"
    CHUNK = 512

    def __init__(self, *, threshold: float = 0.5, sample_rate: int = SAMPLE_RATE) -> None:
        self.threshold = threshold
        self.sample_rate = sample_rate
        self._model = None
        self._buf = np.zeros(0, dtype=np.float32)
        self._last = 0.0
        self._load()

    def _load(self) -> None:
        try:
            import torch  # noqa: F401  (модель-обёртка ждёт torch-тензор)
            from silero_vad import load_silero_vad  # type: ignore

            self._model = load_silero_vad(onnx=True)
            log.info("Silero VAD загружен (onnxruntime, CPU)")
        except Exception as exc:
            log.warning("Silero VAD не загрузился (%s) — будет energy-детектор", exc)
            self._model = None

    @property
    def available(self) -> bool:
        return self._model is not None

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:
                pass

    def probability(self, frame: np.ndarray) -> float:
        """Вероятность речи. Между полными 512-сэмпловыми чанками
        возвращаем последнее значение — чтобы поток оставался синхронным."""
        if self._model is None:
            return 0.0

        import torch

        self._buf = np.concatenate([self._buf, np.asarray(frame, dtype=np.float32)])
        while self._buf.size >= self.CHUNK:
            chunk, self._buf = self._buf[: self.CHUNK], self._buf[self.CHUNK:]
            try:
                tensor = torch.from_numpy(np.ascontiguousarray(chunk, dtype=np.float32))
                self._last = float(self._model(tensor, self.sample_rate).item())
            except Exception as exc:
                log.debug("Silero сбойнул на чанке: %s", exc)
                self._last = 0.0
        return self._last


def build_vad(engine: str, *, threshold: float = 0.5) -> VadEngine:
    """Фабрика движков. Silero при сбое молча откатывается на energy."""
    if engine == "silero":
        vad = SileroVad(threshold=threshold)
        if vad.available:
            return vad
        log.info("Переключаюсь на energy-VAD")
    return EnergyVad()


class SegmentState(str, enum.Enum):
    SILENCE = "silence"
    SPEECH = "speech"


class SpeechSegmenter:
    """Конечный автомат границ фразы поверх VadEngine.

    Логика (с защитой от «дребезга»):
      * речь считается начавшейся после `onset_frames` подряд «громких» кадров;
      * конец фразы — тишина длиной `silence_ms`;
      * фраза короче `min_speech_ms` считается шумом и игнорируется;
      * `max_utterance_s` спасает от бесконечной записи.
    """

    def __init__(
        self,
        vad: VadEngine,
        *,
        sample_rate: int = SAMPLE_RATE,
        frame_ms: int = 20,
        threshold: float = 0.5,
        silence_ms: int = 600,
        min_speech_ms: int = 300,
        max_utterance_s: float = 15.0,
        onset_frames: int = 3,
        speech_threshold: float = 0.6,
    ) -> None:
        self.vad = vad
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.threshold = threshold
        self.frame_s = frame_ms / 1000
        self.silence_frames = max(1, int(silence_ms / frame_ms))
        self.min_speech_frames = max(1, int(min_speech_ms / frame_ms))
        self.max_utterance_frames = int(max_utterance_s / self.frame_s)
        self.onset_frames = onset_frames
        self.speech_threshold = speech_threshold

        self._state = SegmentState.SILENCE
        self._buffer: list[np.ndarray] = []
        self._hot = 0
        self._silence_run = 0

    @property
    def state(self) -> SegmentState:
        return self._state

    @property
    def in_speech(self) -> bool:
        return self._state is SegmentState.SPEECH

    def reset(self) -> None:
        self._state = SegmentState.SILENCE
        self._buffer.clear()
        self._hot = 0
        self._silence_run = 0
        self.vad.reset()

    def feed(self, frame: np.ndarray) -> np.ndarray | None:
        """Обрабатывает один кадр.

        Возвращает `None`, если фраза ещё не закончилась, либо готовую
        фразу целиком (float32) в момент `speech_end`.
        """
        prob = self.vad.probability(frame)
        loud = prob >= self.threshold

        if self._state is SegmentState.SILENCE:
            self._hot = self._hot + 1 if loud else 0
            if self._hot >= self.onset_frames:
                self._state = SegmentState.SPEECH
                self._buffer.clear()
                self._silence_run = 0
                self.vad.reset()
            return None

        # --- внутри фразы ---
        self._buffer.append(frame)
        self._silence_run = 0 if loud else self._silence_run + 1

        too_long = len(self._buffer) >= self.max_utterance_frames
        ended = self._silence_run >= self.silence_frames
        if not (ended or too_long):
            return None

        if too_long and not ended:
            log.info("Достигнут лимит длительности фразы, прерываю")

        self._state = SegmentState.SILENCE
        self._hot = 0
        frame_count = len(self._buffer)
        audio = np.concatenate(self._buffer) if self._buffer else np.zeros(0, dtype=np.float32)
        self._buffer.clear()
        self._silence_run = 0

        # Отбрасываем слишком короткие «слова» — почти всегда это шум.
        # Считаем в кадрах: длительность в кадрах надёжнее, чем деление
        # размера массива на частоту дискретизации.
        if frame_count < self.min_speech_frames:
            log.debug("Фраза %d кадров короче минимума %d — шум",
                      frame_count, self.min_speech_frames)
            self.vad.reset()
            return None

        log.info("Фраза завершена: %.2f с", audio.size / self.sample_rate)
        return audio


async def segment_stream(
    frames: AsyncIterator[np.ndarray],
    segmenter: SpeechSegmenter,
) -> AsyncIterator[np.ndarray]:
    """Превращает поток кадров в поток фраз.

        async for utterance in segment_stream(capture.stream(), segmenter):
            await stt.transcribe(utterance)
    """
    async for frame in frames:
        result = segmenter.feed(frame)
        if result is not None:
            yield result


def has_speech(audio: np.ndarray, *, sample_rate: int = SAMPLE_RATE,
               min_ms: int = 150) -> bool:
    """Быстрая проверка «в аудио вообще есть речь» — для отсева пустых входов."""
    if audio.size < int(sample_rate * min_ms / 1000):
        return False
    return dbfs(audio) > -50.0


#: Историческое имя. Отдельная реализация была точной копией `audio.dbfs`,
#: и две одинаковые формулы в разных слоях рано или поздно разъезжаются.
dbfs_level = dbfs
