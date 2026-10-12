"""Воспроизведение ответа с возможностью прерывания.

Это самый недооценённый модуль ассистента. Правило простое: **пользователь
всегда главнее**. Если он заговорил, ассистент замолкает — иначе невозможно
перебить длинный ответ и диалог превращается в монолог робота.

Поэтому `play()` — не блокирующий вызов, а отменяемая задача:
  * `player.cancel()` останавливает звук мгновенно (буфер очищается);
  * новый ответ (`play`) автоматически прерывает предыдущий;
  * `self_duck` приглушает громкость, когда пользователь начинает говорить.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import numpy as np
import sounddevice as sd

from ..core.errors import AudioError
from .capture import DTYPE, resample
from .devices import resolve_device

log = logging.getLogger(__name__)


class AudioPlayer:
    """Проигрывает PCM и умеет останавливаться по требованию."""

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        device: str | int | None = None,
        volume: float = 1.0,
        speed: float = 1.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.volume = max(0.0, min(1.0, volume))
        self.speed = max(0.5, min(2.0, speed))
        self._device = device
        self._task: asyncio.Task | None = None
        self._playing = False

    # --- управление --------------------------------------------------------

    @property
    def is_playing(self) -> bool:
        return self._playing

    def cancel(self) -> None:
        """Немедленно замолчать. Безопасно вызывать из любого места."""
        self._playing = False
        current = asyncio.current_task()
        # Не отменяем задачу, которая вызывает cancel() сама.
        if self._task and not self._task.done() and self._task is not current:
            self._task.cancel()

    async def play(self, pcm: np.ndarray, *, sample_rate: int | None = None) -> bool:
        """Проигрывает аудио. False — если прервали или звука не было."""
        if pcm is None or pcm.size == 0:
            return False

        current = asyncio.current_task()
        # Прерываем предыдущее воспроизведение. Сравнение с `current`
        # обязательно: без него cancel() отменит задачу, которая сама
        # вызвала play(), и звук оборвётся на первом же блоке.
        if self._task and not self._task.done() and self._task is not current:
            self._task.cancel()
            await asyncio.sleep(0)  # дать задаче завершиться

        self._task = current
        self._playing = True
        rate = sample_rate or self.sample_rate

        try:
            audio = self._prepare(pcm, rate)
            await self._stream_to_speakers(audio)
            return True
        except asyncio.CancelledError:
            log.debug("Воспроизведение прервано")
            raise
        finally:
            if self._task is current:
                self._task = None
            self._playing = False

    async def play_wait(self, pcm: np.ndarray, *, sample_rate: int | None = None) -> bool:
        """`play`, но без отмены текущей задачи (вызов изнутри `play`)."""
        if pcm is None or pcm.size == 0:
            return False
        self._playing = True
        try:
            await self._stream_to_speakers(self._prepare(pcm, sample_rate or self.sample_rate))
            return True
        finally:
            self._playing = False

    # --- внутреннее --------------------------------------------------------

    def _prepare(self, pcm: np.ndarray, rate: int) -> np.ndarray:
        audio = pcm.astype(DTYPE, copy=False)
        if rate != self.sample_rate:
            audio = resample(audio, rate, self.sample_rate)
        if self.volume != 1.0:
            audio = audio * self.volume
        return np.ascontiguousarray(audio, dtype=DTYPE)

    async def _stream_to_speakers(self, audio: np.ndarray) -> None:
        idx = resolve_device(self._device, want_input=False)
        block = max(1, int(self.sample_rate * 0.02))

        try:
            # Именно блокирующий поток (без callback): мы сами передаём блоки
            # через write(). На callback-потоке write() запрещён —
            # PortAudio вернёт "Can't write to a callback stream".
            stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype=DTYPE,
                device=idx,
                blocksize=block,
            )
            stream.start()
        except Exception as exc:
            raise AudioError(f"Не удалось открыть динамики: {exc}") from exc

        try:
            for start in range(0, audio.size, block):
                # Проверяем отмену между блоками: `cancel()` вызывается из
                # другой корутины, и мы обязаны подхватить её между блоками.
                if not self._playing:
                    log.debug("Воспроизведение прервано пользователем")
                    return

                chunk = audio[start:start + block]
                if self._speed_factor != 1.0:
                    # Ускорение/замедление прореживанием кадров внутри блока.
                    idxs = (np.arange(chunk.size) / self._speed_factor).astype(int)
                    idxs = np.clip(idxs, 0, chunk.size - 1)
                    chunk = chunk[idxs]

                if chunk.size < block:
                    # Последний неполный блок дополняем тишиной, иначе
                    # PortAudio может не принять пакет нестандартного размера.
                    chunk = np.pad(chunk, (0, block - chunk.size))

                stream.write(chunk.reshape(-1, 1))
                # Даём event loop обработать отмену и новые события.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AudioError(f"Ошибка воспроизведения: {exc}") from exc
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("Ошибка при закрытии выходного потока", exc_info=True)

    _speed_factor = 1.0

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.5, min(2.0, speed))
        self._speed_factor = 1.0 / self.speed  # быстрее речи -> меньше сэмплов

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(1.0, volume))

    def duck(self, factor: float = 0.35) -> None:
        """Приглушить себя (например, когда заговорил пользователь)."""
        self.set_volume(self.volume * factor)

    def unduck(self) -> None:
        self.set_volume(self.volume / 0.35 if self.volume < 1.0 else 1.0)


class AudioPlayerThreaded:
    """Синхронная обёртка для мест вне event loop (например, для тестов)."""

    def __init__(self, **kwargs) -> None:
        self._player = AudioPlayer(**kwargs)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def play(self, pcm: np.ndarray, sample_rate: int | None = None) -> bool:
        if not self._loop:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._player.play_wait(pcm, sample_rate=sample_rate), self._loop
        )
        try:
            return future.result(timeout=120)
        except Exception:
            log.exception("Поток воспроизведения упал")
            return False
