"""Захват звука с микрофона: непрерывный поток + кольцевой буфер + pre-roll.

Ключевая деталь, которую обычно упускают: если ждать пробуждения, а потом
начать запись, первое слово команды теряется («Синапс **открой** блокнот»).
Поэтому микрофон пишется **всегда**, а в кольцевом буфере хранятся последние
`pre_roll_ms` миллисекунд. В момент активации они приклеиваются к записи.

Особенности реализации:
  * `sounddevice` вызывает `_callback` из своего потока — оттуда нельзя
    трогать asyncio; используется `loop.call_soon_threadsafe` и неблокирующая
    очередь. Переполнение очереди означает, что потребитель не успевает, —
    в этом случае кадры **пропускаются**, а не копятся (иначе растёт задержка
    и «эхо» в ушах).
  * Данные нормализуются: float32 mono 16 кГц.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

from ..core.errors import AudioError, NoAudioDevice
from .devices import resolve_device

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
DTYPE = np.float32


def _resample_factor(src: int, dst: int = SAMPLE_RATE) -> tuple[int, int]:
    """Сокращённый дробный множитель для `resample_poly`."""
    divisor = math.gcd(src, dst)
    return dst // divisor, src // divisor


def _resample_poly(data: np.ndarray, up: int, down: int) -> np.ndarray:
    """Пересемплирование с фильтром, а не линейной интерполяцией.

    Линейная интерполяция на 44100 -> 16000 (сокращение в 2.76 раза)
    оставляет алиасы: они звучат как шум поверх речи. `resample_poly`
    сначала фильтрует, потом сжимает, поэтому гармоники не заворачиваются
    в audible-диапазон. scipy есть в окружении (нужен Silero TTS), но
    импорт делаем ленивым: без него остаётся запасной путь.
    """
    if up == down:
        return data.astype(DTYPE, copy=False)
    try:
        from scipy.signal import resample_poly

        return np.asarray(
            resample_poly(data.astype(np.float64), up, down), dtype=DTYPE)
    except Exception:
        log.debug("scipy недоступен — линейное пересемплирование", exc_info=True)
        return resample(data, len(data) * up // down, 1.0, up, down)


class AudioCapture:
    """Непрерывный захват микрофона.

    Использование:
        cap = AudioCapture()
        await cap.start()
        async for frame in cap.stream():
            ...          # frame: np.float32, mono, 16 кГц, ~20 мс
        await cap.stop()
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        frame_ms: int = 20,
        pre_roll_ms: int = 500,
        device: str | int | None = None,
        latency: str = "high",
        input_rate: int | None = None,
        on_overflow: Callable[[int], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_samples = max(1, int(sample_rate * frame_ms / 1000))
        self._pre_roll_frames = max(1, int(sample_rate * pre_roll_ms / 1000) // self.frame_samples)
        self._device = device
        self.latency = latency
        self._on_overflow = on_overflow

        #: Реальная частота входа. Почти всегда НЕ равна 16 кГц:
        #: у микрофонов это 44.1 или 48 кГц, а мысленно мы всегда
        #: ждём 16 кГц, потому что столько нужно Whisper.
        self.input_rate = input_rate or sample_rate
        self._up = 1
        self._down = 1
        self._resample_buf = np.zeros(0, dtype=DTYPE)

        self._ring: deque[np.ndarray] = deque(maxlen=self._pre_roll_frames)
        self._queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue(maxsize=256)
        self._stream: sd.RawInputStream | sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dropped = 0
        self._captured = 0

    # --- запуск ------------------------------------------------------------

    async def start(self) -> None:
        if self._stream is not None:
            return
        self._loop = asyncio.get_running_loop()
        idx = resolve_device(self._device, want_input=True)

        # Частота берётся у САМОГО устройства, а не из конфига.
        # Ни один реальный микрофон не умеет 16 кГц — там всегда 44.1 или
        # 48. Просить драйвер пересемплировать самому нельзя: на MME это
        # даёт кашу, которую Whisper слышит как не-речь (no_speech_prob
        # около 0.8), а грубый Silero — как речь. Поэтому снимаем в
        # нативной частоте и пересемплируем сами.
        self.input_rate = self._probe_rate(idx)
        self._up, self._down = _resample_factor(self.input_rate, self.sample_rate)
        self._resample_buf = np.zeros(0, dtype=DTYPE)

        def _callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            # Этот код выполняется в потоке Portaudio. Здесь нельзя await,
            # нельзя блокировать и нельзя бросать исключения наружу.
            if status:  # underflow/overflow — заполняем лакуны нулями
                log.warning("Portaudio: %s", status)

            block = np.asarray(indata, dtype=DTYPE).reshape(-1)
            for frame in self._to_target_rate(block):
                self._ring.append(frame)
                self._captured += len(frame)

                if self._loop is None:
                    continue
                try:
                    self._loop.call_soon_threadsafe(self._offer, frame)
                except RuntimeError:
                    # Цикл уже закрыт (приложение завершается) — тихо выходим.
                    pass

        try:
            self._stream = sd.InputStream(
                samplerate=self.input_rate,
                device=idx,
                channels=1,
                dtype=DTYPE,
                callback=_callback,
                latency=self.latency,
            )
            self._stream.start()
        except Exception as exc:
            raise NoAudioDevice(str(exc)) from exc

        name = sd.query_devices(idx)["name"] if idx is not None else "системный микрофон"
        # Задержку в лог: если буфер мал, а поток event loop занят — будут
        # переполнения, и без этого числа диагностика превращается в угадывание.
        try:
            actual = self._stream.latency * 1000
        except Exception:
            actual = float("nan")
        log.info(
            "Микрофон запущен: %s, вход %d Гц -> %d Гц, фрейм %d мс, "
            "pre-roll %d мс, буфер %.0f мс",
            name, self.input_rate, self.sample_rate, self.frame_ms,
            self._pre_roll_frames * self.frame_ms, actual,
        )

    def _probe_rate(self, idx) -> int:
        """Нативная частота устройства ввода."""
        if idx is not None:
            try:
                return int(sd.query_devices(idx)["default_samplerate"])
            except Exception:
                log.debug("Не удалось узнать частоту устройства %s", idx,
                          exc_info=True)
        try:
            return int(sd.query_devices(kind="input")["default_samplerate"])
        except Exception:
            return self.sample_rate

    def _to_target_rate(self, block: np.ndarray) -> list[np.ndarray]:
        """Пересемплирует блок и нарезает на кадры по 20 мс.

        Остаток сохраняется между вызовами: иначе на стыках блоков
        появились бы щелчки, а на накоплении — рассинхронизация времени.
        """
        if self._up == 1 and self._down == 1:
            ready = block
            self._resample_buf = np.zeros(0, dtype=DTYPE)
        else:
            buf = np.concatenate([self._resample_buf, block])
            # Сколько выходных сэмплов можно получить целиком из входа.
            usable = (buf.size * self._up) // self._down
            if usable == 0:
                self._resample_buf = buf
                return []

            chunk_in = usable * self._down // self._up
            ready = _resample_poly(buf[:chunk_in], self._up, self._down)
            ready = ready[:usable]
            self._resample_buf = buf[chunk_in:].copy()

        frames: list[np.ndarray] = []
        size = self.frame_samples
        usable_len = (ready.size // size) * size
        for i in range(0, usable_len, size):
            frames.append(np.ascontiguousarray(ready[i:i + size], dtype=DTYPE))
        if usable_len < ready.size:
            self._resample_buf = np.concatenate(
                [self._resample_buf, ready[usable_len:]]).astype(DTYPE)
        return frames

    def _offer(self, frame: np.ndarray) -> None:  # вызывается уже в event loop
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Потребитель отстал (тяжёлый STT). Пропускаем кадр, чтобы не копить
            # задержку: лучше короткая потеря, чем «эхо» с отставанием в секунду.
            self._dropped += len(frame)
            if self._on_overflow:
                self._on_overflow(self._dropped)

    async def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            log.debug("Ошибка при остановке потока звука", exc_info=True)
        finally:
            self._stream = None
            self._loop = None
        log.info("Микрофон остановлен (пропущено кадров: %d)", self._dropped)

    # --- чтение ------------------------------------------------------------

    async def stream(self) -> AsyncIterator[np.ndarray]:
        """Бесконечный поток фреймов. Останавливается только по `stop()`."""
        while True:
            frame = await self._queue.get()
            if frame is None:  # сигнал завершения
                break
            yield frame

    def preroll(self) -> np.ndarray:
        """Аудио, записанное ДО текущего момента (буфер активации)."""
        if not self._ring:
            return np.zeros(0, dtype=DTYPE)
        return np.concatenate(list(self._ring))

    def drain_preroll(self) -> np.ndarray:
        """Забрать pre-roll и очистить буфер — чтобы не склеить его дважды."""
        data = self.preroll()
        self._ring.clear()
        return data

    async def record(self, max_seconds: float) -> np.ndarray:
        """Записать фиксированный кусок (для тестов и `--say-to-file`)."""
        chunks = [self.drain_preroll()]
        needed = int(self.sample_rate * max_seconds)
        got = 0
        async for frame in self.stream():
            chunks.append(frame)
            got += len(frame)
            if got >= needed:
                break
        audio = np.concatenate(chunks)
        return audio[: max(needed, 0)] if needed else audio

    # --- статистика --------------------------------------------------------

    @property
    def dropped_samples(self) -> int:
        return self._dropped

    @property
    def captured_samples(self) -> int:
        return self._captured

    def stats(self) -> dict[str, float]:
        total = max(1, self._captured)
        return {
            "captured_s": round(self._captured / self.sample_rate, 1),
            "dropped_s": round(self._dropped / self.sample_rate, 2),
            "drop_pct": round(100 * self._dropped / total, 3),
        }


# --- утилиты обработки ------------------------------------------------------


def normalize_peak(audio: np.ndarray, target: float = 0.95) -> np.ndarray:
    """Поднимает тихий голос до комфортной громкости.

    Whisper обучен на разумной громкости; запись с дешёвого микрофона часто
    слишком тихая, и качество распознавания падает сильнее, чем от шума.
    """
    if audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak < 1e-6:  # тишина — усиливать нечего
        return audio
    return (audio * (target / peak)).astype(DTYPE)


def rms(audio: np.ndarray) -> float:
    """Громкость (среднеквадратичное значение). Основа детектора речи."""
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64), dtype=np.float64)))


def dbfs(audio: np.ndarray) -> float:
    """Громкость в дБ относительно полной шкалы."""
    level = rms(audio)
    return 20 * np.log10(level) if level > 1e-9 else -120.0


def trim_silence(audio: np.ndarray, threshold_db: float = -45.0, margin_ms: int = 150,
                 window_ms: int = 20, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Убирает тишину по краям фразы — ускоряет распознавание.

    Порог сравнивается с громкостью ОКНА, а не с амплитудой каждого
    сэмпла. Разница принципиальная: в речи отдельные сэмплы почти всегда
    близки к нулю (вокализованные интервалы), поэтому проверка «амплитуда
    больше порога» отрезает тихое начало слова.

    Измерено на фикстурах и на живой записи с тихим микрофоном:
      * по амплитуде сэмпла — терялось 23-36% фразы, а на записи с фоном
        комнаты до 81%: «Сделай громкость громче» превращалось
        в «Громкость громче», а «который час» падал по уверенности;
      * по громкости окна — остаётся целиком.
    """
    if audio.size == 0:
        return audio

    win = max(1, int(sample_rate * window_ms / 1000))
    if audio.size < win:
        return audio

    frames = audio.size // win
    if frames == 0:
        return audio

    # Громкость каждого окна, а не пиковые сэмплы внутри него.
    blocks = audio[:frames * win].reshape(frames, win)
    envelope = np.sqrt(np.mean(np.square(blocks, dtype=np.float64), axis=1))

    thresh = 10 ** (threshold_db / 20)
    loud = np.flatnonzero(envelope > thresh)
    if loud.size == 0:
        return audio

    margin = int(sample_rate * margin_ms / 1000)
    start = max(0, int(loud[0]) * win - margin)
    end = min(audio.size, (int(loud[-1]) + 1) * win + margin)
    return audio[start:end]


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Линейное пересемплирование. Достаточно для речи 16↔24 кГц.

    Для серьёзного качества лучше scipy.signal.resample_poly, но он тянет
    лишнюю зависимость, а для TTS-воспроизведения разница не слышна.
    """
    if src_rate == dst_rate or audio.size == 0:
        return audio
    target_len = int(round(audio.size * dst_rate / src_rate))
    if target_len <= 0:
        return np.zeros(0, dtype=DTYPE)
    idx = np.linspace(0, audio.size - 1, target_len, dtype=np.float64)
    return np.interp(idx, np.arange(audio.size), audio).astype(DTYPE)


def to_int16(audio: np.ndarray) -> np.ndarray:
    """float32 [-1, 1] -> int16 (для Vosk и файлов)."""
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)


def write_wav(path: str, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
    """float32 [-1, 1] -> WAV 16 бит моно. Только stdlib, без scipy."""
    import wave

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pcm = to_int16(audio)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return str(path)


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Читает WAV -> (float32 mono, sample_rate). Только stdlib, без scipy."""
    import wave

    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        width = wf.getsampwidth()
        channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())

    if width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(DTYPE) / 32767.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(DTYPE) / 2147483647.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(DTYPE) - 128.0) / 128.0
    else:  # pragma: no cover
        raise AudioError(f"Неподдерживаемая ширина WAV-сэмпла: {width} байт")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data.astype(DTYPE), rate


def record_to_file(path: str, seconds: float, *, sample_rate: int = SAMPLE_RATE,
                   device: str | int | None = None) -> str:
    """Синхронная запись в WAV — для создания тестовных фикстур и отладки."""
    idx = resolve_device(device, want_input=True)
    frames = int(sample_rate * seconds)
    log.info("Записываю %.1f с в %s", seconds, path)
    data = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32", device=idx)
    sd.wait()
    audio = data.reshape(-1)
    write_wav(path, audio, sample_rate)
    log.info("Готово: %s (RMS=%.4f, %d дБ)", path, rms(audio), dbfs(audio))
    return path


async def wait_for_silence(capture: AudioCapture, *, max_seconds: float = 3.0) -> np.ndarray:
    """Читает поток, пока не наберётся `max_seconds` тишины (для отладки)."""
    window: list[np.ndarray] = []
    silent_for = 0.0
    frame_s = capture.frame_ms / 1000
    async for frame in capture.stream():
        window.append(frame)
        silent_for = silent_for + frame_s if rms(frame) < 0.01 else 0.0
        if silent_for >= max_seconds:
            break
    return np.concatenate(window) if window else np.zeros(0, dtype=DTYPE)
