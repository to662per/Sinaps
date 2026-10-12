"""Источники аудио для контура прослушивания.

Абстракция нужна по двум причинам:
  * на машине может не быть микрофона (headless, VM, контейнер) — но конвейер
    всё равно надо проверять;
  * тестам нужен детерминированный вход, а не запись с реального устройства.

`AudioCapture` (живой микрофон) и `FileAudioSource` (wav-файл) реализуют
один и тот же интерфейс, поэтому `ListeningPipeline` их не различает.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from ..core.errors import AudioError
from .capture import SAMPLE_RATE, read_wav, resample, rms, to_int16

log = logging.getLogger(__name__)


class FileAudioSource:
    """Имитирует микрофон, отдавая заранее записанный wav-файл.

    Поведение повторяет живой захват: кадры по `frame_ms`, кольцевой буфер с
    pre-roll, остановка через `stop()`. Поэтому VAD и STT работают с ним
    ровно так же, как с настоящим микрофоном.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        sample_rate: int = SAMPLE_RATE,
        frame_ms: int = 20,
        pre_roll_ms: int = 500,
        loop: bool = False,
        speed: float = 1.0,
    ) -> None:
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.speed = speed
        self.loop = loop

        audio, file_rate = read_wav(self.path)
        if file_rate != sample_rate:
            audio = resample(audio, file_rate, sample_rate)
        self._audio = np.ascontiguousarray(audio, dtype=np.float32)
        self._frame_samples = max(1, int(sample_rate * frame_ms / 1000))
        self._pre_roll_frames = max(1, int(sample_rate * pre_roll_ms / 1000) // self._frame_samples)
        self._ring: deque[np.ndarray] = deque(maxlen=self._pre_roll_frames)
        self._queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue(maxsize=256)
        self._position = 0
        self._stopped = False

        log.info(
            "Источник аудио: %s (%.2f с, %d Гц, RMS=%.4f)",
            self.path.name, len(self._audio) / sample_rate, sample_rate, rms(self._audio),
        )

    # --- интерфейс микрофона ------------------------------------------------

    async def start(self) -> None:
        self._stopped = False
        self._position = 0
        self._queue = asyncio.Queue(maxsize=256)
        self._task = asyncio.create_task(self._pump(), name="sinaps-file-audio")
        log.info("Файл-источник запущен")

    async def stop(self) -> None:
        self._stopped = True
        task = getattr(self, "_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._queue.put(None)
        log.info("Файл-источник остановлен")

    async def stream(self) -> AsyncIterator[np.ndarray]:
        while True:
            frame = await self._queue.get()
            if frame is None:
                break
            yield frame

    def preroll(self) -> np.ndarray:
        if not self._ring:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(list(self._ring))

    def drain_preroll(self) -> np.ndarray:
        data = self.preroll()
        self._ring.clear()
        return data

    async def record(self, max_seconds: float) -> np.ndarray:
        return self._audio[: int(self.sample_rate * max_seconds)]

    @property
    def duration_s(self) -> float:
        return len(self._audio) / self.sample_rate

    @property
    def stats(self) -> dict[str, float]:
        return {"captured_s": self.duration_s, "dropped_s": 0.0, "drop_pct": 0.0}

    # --- внутреннее --------------------------------------------------------

    def _make_frame(self) -> np.ndarray | None:
        if self._position >= len(self._audio):
            if not self.loop:
                return None
            self._position = 0
            # Тишина между повторами, чтобы VAD не склеивал слова встык.
            return np.zeros(self._frame_samples * int(1.5 / self.frame_ms), dtype=np.float32)

        frame = self._audio[self._position:self._position + self._frame_samples]
        if frame.size < self._frame_samples:
            frame = np.pad(frame, (0, self._frame_samples - frame.size))
        self._position += self._frame_samples
        return frame

    async def _pump(self) -> None:
        """Кладёт кадры в очередь с темпом реального времени."""
        frame_seconds = self.frame_ms / 1000 / max(0.25, self.speed)
        while not self._stopped:
            frame = self._make_frame()
            if frame is None:
                break
            self._ring.append(frame)
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass
            await asyncio.sleep(frame_seconds)
        await self._queue.put(None)


def make_source(
    path: str | Path | None,
    *,
    sample_rate: int = SAMPLE_RATE,
    frame_ms: int = 20,
    pre_roll_ms: int = 500,
    device: str | int | None = None,
    latency: str = "high",
    loop: bool = False,
    speed: float = 1.0,
):
    """Файл -> FileAudioSource, иначе -> живой микрофон."""
    if path:
        p = Path(path)
        if not p.exists():
            raise AudioError(f"Аудиофайл не найден: {p}")
        return FileAudioSource(
            p, sample_rate=sample_rate, frame_ms=frame_ms,
            pre_roll_ms=pre_roll_ms, loop=loop, speed=speed,
        )

    from .capture import AudioCapture

    return AudioCapture(
        sample_rate=sample_rate, frame_ms=frame_ms,
        pre_roll_ms=pre_roll_ms, device=device, latency=latency,
    )


def write_wav_bytes(audio: np.ndarray) -> bytes:
    """float32 -> WAV-байты (для тестов, без записи на диск)."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(to_int16(audio).tobytes())
    return buf.getvalue()
