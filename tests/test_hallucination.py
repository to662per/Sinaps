"""Защита от галлюцинаций Whisper.

Контекст. В бою на попытку открыть блокнот прозвучало:

    'Субтитры субтитров Н.Новикова' (conf=0.03)

Это не ошибка распознавания, а поведение модели: на входе без речи
Whisper не молчит, а выбирает наиболее вероятное правдоподобное
продолжение. Измерено на этой модели (small, CPU):

    живая речь      conf 0.53 - 0.92
    выдумки         conf 0.00 - 0.06

Два барьера: по громкости до модели (дешевле) и по уверенности после.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from sinaps.audio.capture import AudioCapture, _resample_factor, dbfs
from sinaps.conversation.listening import ListeningPipeline
from sinaps.core.bus import EventBus, EventType
from sinaps.core.config import SinapsConfig
from sinaps.core.metrics import Metrics
from sinaps.core.session import SessionController, State
from sinaps.stt.base import STTEngine, Transcript

SR = 16000


def noise(seconds: float, level: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    return (rng.standard_normal(n) * level).astype(np.float32)


def speech_like(seconds: float = 1.5) -> np.ndarray:
    """Что-то громкое и непрерывное — как настоящая речь по уровню."""
    n = int(SR * seconds)
    t = np.arange(n) / SR
    wave = np.sin(2 * np.pi * 220 * t) * 0.3
    return (wave + np.random.default_rng(1).standard_normal(n) * 0.02).astype(np.float32)


class FakeSTT(STTEngine):
    """Подменяет модель: заранее заданный текст и уверенность."""

    name = "fake"

    def __init__(self, text: str, confidence: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._text = text
        self._confidence = confidence
        self.calls: list[np.ndarray] = []

    def _load(self) -> None:
        return None

    def _recognize(self, audio: np.ndarray) -> Transcript:
        # Настоящий движок на пустом массиве выходит ДО вызова модели.
        # Заглушка повторяет это, иначе тест измерял бы не то.
        if audio.size == 0:
            return Transcript(text="", confidence=0.0, engine=self.name)
        self.calls.append(audio)
        return Transcript(text=self._text, confidence=self._confidence,
                          engine=self.name)


# --- барьер по громкости ---------------------------------------------------


def test_quiet_audio_never_reaches_the_model():
    """Тишина и шум не должны даже загружать GPU.

    Проверено на реальной модели: на -60 dBFS она выдаёт «СПОКОЙНАЯ
    МУЗЫКА», тратя на это ~0.5 с. Отсекать надо до вызова.
    """
    stt = FakeSTT("СПОКОЙНАЯ МУЗЫКА", 0.06)

    result = stt.transcribe_sync(noise(2.0, 0.001))    # -60 dBFS

    assert stt.calls == [], "модель не должна вызываться на тишине"
    assert result.is_empty


def test_loud_audio_still_reaches_the_model():
    """Барьер не должен съесть настоящую речь."""
    stt = FakeSTT("открой блокнот", 0.88)

    result = stt.transcribe_sync(speech_like())

    assert len(stt.calls) == 1, "речь обязана дойти до модели"
    assert result.text == "открой блокнот"


def test_dbfs_threshold_is_configurable():
    """Пользователь может разрешить более тихий микрофон."""
    strict = FakeSTT("тихо", 0.9, min_dbfs=-30.0)
    strict.transcribe_sync(noise(2.0, 0.003))       # -50 dBFS
    assert strict.calls == [], "порог -30 обязан отбрасывать -50 dBFS"

    lenient = FakeSTT("тихо", 0.9, min_dbfs=-60.0)
    lenient.transcribe_sync(noise(2.0, 0.003))
    assert len(lenient.calls) == 1, "порог -60 обязан пропускать -50 dBFS"


def test_threshold_can_be_disabled():
    """-120 — это «не проверять»: полезно для отладки."""
    stt = FakeSTT("что угодно", 0.01, min_dbfs=-120.0)
    stt.transcribe_sync(np.zeros(SR, dtype=np.float32))

    assert len(stt.calls) == 1


# --- барьер по уверенности -------------------------------------------------


class FakeCapture:
    """Заглушка микрофона: отдаёт заранее заданные фразы."""

    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio
        self.sample_rate = SR
        self.frame_ms = 20

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def stream(self):
        chunk = int(SR * self.frame_ms / 1000)
        for i in range(0, self.audio.size, chunk):
            yield self.audio[i:i + chunk]


async def run_once(text: str, confidence: float, **kwargs):
    """Прогоняет фразу через конвейер и возвращает итоговое событие."""
    audio = speech_like(1.5)
    bus = EventBus()
    session = SessionController(bus)
    seen: list = []

    async def _capture(event):
        seen.append(event)

    bus.on(EventType.TRANSCRIPT_FINAL)(_capture)

    pipeline = ListeningPipeline(
        capture=FakeCapture(audio),
        stt=FakeSTT(text, confidence, min_dbfs=-120.0),
        vad_engine="energy",
        session=session,
        bus=bus,
        metrics=Metrics(),
        silence_ms=100,
        min_speech_ms=10,
        **kwargs,
    )

    # Сегментер не нужен: интересует только решение по результату STT.
    await session.transition(State.LISTENING, reason="test")
    await pipeline._on_utterance(audio)  # noqa: SLF001

    return seen


@pytest.mark.asyncio
async def test_hallucination_is_rejected():
    """Реальный случай из лога: conf=0.03 не должен стать командой."""
    seen = await run_once("Субтитры субтитров Н.Новикова", 0.03)

    assert len(seen) == 1
    event = seen[0]
    assert event.payload["text"] == "", "галлюцинация не должна попасть дальше"
    assert event.payload.get("rejected") == "low_confidence"


@pytest.mark.asyncio
async def test_real_command_passes():
    """Настоящая фраза с уверенностью 0.88 проходит без помех."""
    seen = await run_once("Открой блокнот", 0.88)

    assert len(seen) == 1
    assert seen[0].payload["text"] == "Открой блокнот"
    assert "rejected" not in seen[0].payload


@pytest.mark.asyncio
async def test_weakest_real_command_passes():
    """Нижняя граница живой речи по замерам — 0.53. Порог 0.25 её не режет."""
    seen = await run_once("который час", 0.53)
    assert seen[0].payload["text"] == "который час"


@pytest.mark.asyncio
async def test_threshold_is_configurable():
    """Строгий порог режет то, что мягкий пропускает."""
    soft = await run_once("сомнительно", 0.10, min_confidence=0.25)
    assert soft[0].payload["text"] == ""

    hard = await run_once("сомнительно", 0.10, min_confidence=0.05)
    assert hard[0].payload["text"] == "сомнительно"


@pytest.mark.asyncio
async def test_empty_result_is_not_double_counted():
    """Пустой результат и отброшенный — разные причины, но обе не команда."""
    seen = await run_once("", 0.0)
    assert seen[0].payload["text"] == ""
    assert seen[0].payload.get("rejected") != "low_confidence"


# --- pre-roll: первое слово фразы ------------------------------------------
#
# Сегментер начинает буферизовать только после трёх кадров, признанных
# речью, — самое начало фразы к этому моменту уже минуло. Кольцевой буфер
# записи на 500 мс существует ровно для этого, но подклеивать его никто
# не делал. Измерено на фикстурах: без pre-roll «Сделай громкость громче»
# распознавалось как «Громкость громче» (conf 0.47), с ним — полностью
# (conf 0.91).


class RingCapture:
    """Источник с кольцевым буфером — как настоящий микрофон."""

    def __init__(self, audio: np.ndarray, preroll_ms: int = 500) -> None:
        self.audio = audio
        self.sample_rate = SR
        self.frame_ms = 20
        self._ring_frames = max(1, int(SR * preroll_ms / 1000) // 320)
        self._ring: list[np.ndarray] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def stream(self):
        for i in range(0, self.audio.size, 320):
            frame = self.audio[i:i + 320]
            self._ring.append(frame)
            if len(self._ring) > self._ring_frames:
                self._ring.pop(0)
            yield frame

    def drain_preroll(self) -> np.ndarray:
        data = np.concatenate(self._ring) if self._ring else np.zeros(0, np.float32)
        self._ring.clear()
        return data

    def preroll(self) -> np.ndarray:
        return np.concatenate(self._ring) if self._ring else np.zeros(0, np.float32)


class ScriptedVad:
    """VAD с заданным расписанием: сначала речь, потом тишина.

    EnergyVad по устройству калибруется под первый звук и принимает
    постоянный сигнал за шумовой фон — для теста нужен предсказуемый
    детектор, а не обучаемый.
    """

    name = "scripted"

    def __init__(self, speech_frames: int = 25) -> None:
        self.speech_frames = speech_frames
        self.seen = 0

    def reset(self) -> None:
        return None

    def probability(self, frame: np.ndarray) -> float:
        self.seen += 1
        return 1.0 if self.seen <= self.speech_frames else 0.0


def _pipeline_with_ring(capture, stt, speech_frames: int = 25, **kwargs):
    bus = EventBus()
    session = SessionController(bus)
    captured: list = []

    async def _on_transcript(event):
        captured.append(event)

    bus.on(EventType.TRANSCRIPT_FINAL)(_on_transcript)

    pipeline = ListeningPipeline(
        capture=capture, stt=stt, vad_engine="energy",
        session=session, bus=bus, metrics=Metrics(),
        silence_ms=100, min_speech_ms=10, require_activation=False,
        min_confidence=0.0, **kwargs,
    )
    pipeline.segmenter.vad = ScriptedVad(speech_frames)
    pipeline.segmenter.reset()
    return pipeline, captured, session


async def _drive(pipeline, capture, session, frames: int = 200):
    """Прогоняет кадры через конвейер, как это делает `_run`."""
    await session.transition(State.LISTENING, reason="test")
    sent = 0
    async for frame in capture.stream():
        await pipeline._on_frame(frame)  # noqa: SLF001
        sent += 1
        if sent >= frames:
            break
    return sent


@pytest.mark.asyncio
async def test_preroll_is_attached_to_the_utterance():
    """К фразе приклеивается аудио, накопленное до обнаружения речи."""
    stt = FakeSTT("Сделай громче", 0.9, min_dbfs=-120.0)
    audio = np.ones(SR, dtype=np.float32) * 0.2

    capture = RingCapture(audio)
    # Речь длится 25 кадров (500 мс) после старта; кольцо наполняется
    # раньше, поэтому часть фразы живёт только в pre-roll.
    pipeline, captured, session = _pipeline_with_ring(capture, stt, speech_frames=10)
    await _drive(pipeline, capture, session, frames=80)

    assert captured, "фраза должна дойти до распознавания"
    assert stt.calls, "модель должна была получить аудио"

    got = stt.calls[0]
    # Сегментер увидел лишь 10 кадров (200 мс). С кольцом к модели должно
    # уйти заметно больше — иначе начало фразы потеряно.
    assert got.size > SR // 4, f"аудио подозрительно короткое: {got.size/SR:.2f}с"


@pytest.mark.asyncio
async def test_preroll_survives_a_soft_onset():
    """Начало фразы тише порога VAD — pre-roll его спасает.

    Именно этот случай стоил слова «Сделай»: первые сотни миллисекунд
    тихие, три кадра речи набираются позже, и без кольца фраза
    начиналась с середины слова.
    """
    stt = FakeSTT("Сделай громче", 0.9, min_dbfs=-120.0)
    audio = np.ones(SR, dtype=np.float32) * 0.2

    capture = RingCapture(audio)
    pipeline, captured, session = _pipeline_with_ring(capture, stt, speech_frames=10)
    await _drive(pipeline, capture, session, frames=80)

    assert captured, "фраза должна распознаться"
    assert captured[0].payload["text"] == "Сделай громче"


@pytest.mark.asyncio
async def test_preroll_is_drained_not_copied():
    """Буфер забирается, а не копируется: повторов в аудио быть не должно."""
    stt = FakeSTT("команда", 0.9, min_dbfs=-120.0)
    capture = RingCapture(np.ones(SR, dtype=np.float32) * 0.2)
    pipeline, _captured, _session = _pipeline_with_ring(capture, stt)

    # Наполняем кольцо вручную: тест про сам забор, а не про VAD.
    async for _frame in capture.stream():
        break

    first = pipeline._take_preroll()   # noqa: SLF001
    second = pipeline._take_preroll()  # noqa: SLF001

    assert first is not None and first.size > 0
    assert second is None, "после забора кольцо должно быть пустым"


@pytest.mark.asyncio
async def test_capture_without_preroll_does_not_break():
    """Источник без кольца (файл, заглушка) не должен ломать конвейер."""

    class NoRing:
        sample_rate = SR
        frame_ms = 20

        async def start(self): ...
        async def stop(self): ...

    stt = FakeSTT("команда", 0.9, min_dbfs=-120.0)
    pipeline, _captured, _session = _pipeline_with_ring(NoRing(), stt)

    assert pipeline._take_preroll() is None  # noqa: SLF001
    audio = np.ones(320, dtype=np.float32) * 0.2
    assert pipeline._with_preroll(audio) is audio  # noqa: SLF001


# --- обрезка тишины -------------------------------------------------------
#
# Порог сравнивался с амплитудой КАЖДОГО сэМПЛА, а не с громкостью окна.
# В речи отдельные сэмплы почти всегда близки к нулю (вокализованные
# интервалы), поэтому тихое начало слова уходило под нож: на фикстурах
# терялось 23-36% фразы, на записи с фоном комнаты — до 81%.


def test_trim_keeps_quiet_onset():
    """Тихое начало слова должно выжить обрезку."""
    from sinaps.audio.capture import trim_silence

    sr = SR
    # Первая треть почти тихая, дальше громкая речь.
    n = sr // 2
    audio = np.concatenate([
        np.sin(np.arange(n) / sr * 2 * np.pi * 200) * 0.004,   # -48 дБ
        np.sin(np.arange(n) / sr * 2 * np.pi * 200) * 0.5,    # -6 дБ
    ]).astype(np.float32)

    out = trim_silence(audio, sample_rate=sr)
    assert out.size > sr * 0.4, (
        f"громкая часть не должна теряться: осталось {out.size/sr:.2f}с")


def test_trim_is_independent_of_background_level():
    """Фон перед фразой не должен менять результат обрезки.

    На живой записи с тихим микрофоном это ломало всё: добавление
    полусекунды комнатного шума меняло итог с 1.09 с на 2.06 с.
    """
    from sinaps.audio.capture import trim_silence

    sr = SR
    speech = (np.sin(np.arange(sr) / sr * 2 * np.pi * 200) * 0.3).astype(np.float32)

    for level in (0.0005, 0.002, 0.01):
        room = (np.random.default_rng(0).standard_normal(sr // 2) * level
                ).astype(np.float32)
        out = trim_silence(np.concatenate([room, speech]), sample_rate=sr)
        assert out.size > sr * 0.7, (
            f"фон {level} исказил обрезку: осталось {out.size/sr:.2f}с")


def test_trim_keeps_silence_when_whole_input_is_quiet():
    """Совсем тихий вход не должен превращаться в пустой."""
    from sinaps.audio.capture import trim_silence

    audio = np.full(SR, 1e-5, dtype=np.float32)
    out = trim_silence(audio, sample_rate=SR)
    assert out.size == audio.size


def test_trim_handles_empty_and_tiny_input():
    from sinaps.audio.capture import trim_silence

    assert trim_silence(np.zeros(0, np.float32), sample_rate=SR).size == 0
    tiny = np.full(10, 0.1, dtype=np.float32)
    assert trim_silence(tiny, sample_rate=SR).size == tiny.size


# --- пересемплирование входа ------------------------------------------------
#
# Ни один реальный микрофон не умеет 16 кГц: у всех 44.1 или 48 кГц.
# Просить драйвер пережать самому нельзя — на MME получается каша,
# которую Whisper слышит как не-речь (no_speech_prob около 0.8), а грубый
# Silero — как речь. Поэтому снимаем в нативной частоте и жмём сами.


class _CaptureForTest(AudioCapture):
    def __init__(self, native_rate):
        super().__init__(sample_rate=SR, frame_ms=20)
        self.input_rate = native_rate
        self._up, self._down = _resample_factor(native_rate, SR)
        self._resample_buf = np.zeros(0, dtype=np.float32)


def _run_resampler(native_rate, seconds=1.0, freq=300.0, block_ms=20):
    cap = _CaptureForTest(native_rate)
    n = int(native_rate * seconds)
    t = np.arange(n) / native_rate
    sig = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    frames = []
    block = int(native_rate * block_ms / 1000)
    for i in range(0, n, block):
        frames.extend(cap._to_target_rate(sig[i:i + block]))
    return np.concatenate(frames) if frames else np.zeros(0, np.float32)


@pytest.mark.parametrize("native", [44100, 48000])
def test_resampling_keeps_duration(native):
    """Скорость не должна «уплывать»: 2 секунды должны остаться 2 секундами."""
    y = _run_resampler(native, seconds=2.0)
    expected = 2.0
    assert abs(y.size / SR - expected) < 0.01, (
        f"{native} Гц -> {y.size/SR:.3f} с вместо {expected}")


@pytest.mark.parametrize("native", [44100, 48000])
def test_resampling_preserves_frequency(native):
    """Тон должен остаться тоном той же частоты."""
    y = _run_resampler(native, seconds=1.0, freq=300.0)
    spec = np.abs(np.fft.rfft(y * np.hanning(y.size)))
    peak = np.fft.rfftfreq(y.size, 1 / SR)[int(np.argmax(spec))]
    assert abs(peak - 300.0) < 5.0, f"частота уехала: {peak:.0f} Гц"


@pytest.mark.parametrize("native", [44100, 48000])
def test_resampling_has_no_discontinuities(native):
    """Стыки блоков не должны щелкать.

    Если буфер между вызовами теряется, на границах появляются скачки —
    и для Whisper это шум поверх речи.
    """
    y = _run_resampler(native, seconds=1.0, freq=300.0)
    jumps = int(np.sum(np.abs(np.diff(y)) > 0.1))
    assert jumps == 0, f"щелчков на стыках блоков: {jumps}"


@pytest.mark.parametrize("native", [44100, 48000])
def test_resampling_output_is_frame_aligned(native):
    """На выходе должны быть ровно кадры по 20 мс."""
    cap = _CaptureForTest(native)
    n = int(native * 0.5)
    block = int(native * 0.02)
    for i in range(0, n, block):
        for frame in cap._to_target_rate(
                np.zeros(block, dtype=np.float32)):
            assert frame.size == cap.frame_samples


def test_resampling_is_skipped_when_rate_matches():
    """При 16 кГц пересемплирование не нужно и не должно ломать сигнал."""
    cap = _CaptureForTest(SR)
    assert (cap._up, cap._down) == (1, 1)
    block = np.full(cap.frame_samples, 0.5, dtype=np.float32)
    frames = cap._to_target_rate(block)
    assert len(frames) == 1
    assert np.allclose(frames[0], block)


def test_resampling_factor_is_reduced():
    """Множитель должен быть сокращён, иначе считаем в разы больше нужного."""
    assert _resample_factor(44100, 16000) == (160, 441)
    assert _resample_factor(48000, 16000) == (1, 3)


def test_unknown_device_rate_is_known():
    """Если частоту узнать не удалось, работаем на 16 кГц, а не падаем."""
    cap = AudioCapture()
    assert cap._probe_rate(999999) > 0


# --- сторож сессии ---------------------------------------------------------
#
# Регрессия: сессия застревала в THINKING навсегда, и ассистент переставал
# реагировать до перезапуска.


class _NoRing:
    """Источник-заглушка: конвейеру нужны только rate и размер кадра."""

    sample_rate = SR
    frame_ms = 20

    async def start(self): ...
    async def stop(self): ...


def _make_watchdog_pipeline(session, stuck_timeout):
    pipeline, _captured, _session = _pipeline_with_ring(_NoRing(), FakeSTT("x", 0.9))
    pipeline.session = session
    pipeline.stuck_timeout_s = stuck_timeout
    return pipeline


@pytest.mark.asyncio
async def test_watchdog_resets_stuck_session():
    """Сессия, зависшая в THINKING, должна быть сброшена сама."""
    from sinaps.core.session import State as S

    bus = EventBus()
    session = SessionController(bus)
    await session.transition(S.IDLE, reason="test")
    await session.transition(S.LISTENING, reason="test")
    await session.transition(S.TRANSCRIBING, reason="test")
    await session.transition(S.THINKING, reason="test")
    session._since -= 999.0          # делаем вид, что сидим тут очень долго

    pipeline = _make_watchdog_pipeline(session, 0.01)
    task = asyncio.create_task(pipeline._watchdog())
    await asyncio.sleep(1.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.state is S.IDLE, "зависшая сессия должна быть сброшена"


@pytest.mark.asyncio
async def test_watchdog_leaves_healthy_session_alone():
    """Активный, но не зависший диалог сторож трогать не должен."""
    from sinaps.core.session import State as S

    bus = EventBus()
    session = SessionController(bus)
    await session.transition(S.LISTENING, reason="test")

    pipeline = _make_watchdog_pipeline(session, 30.0)
    task = asyncio.create_task(pipeline._watchdog())
    await asyncio.sleep(1.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.state is S.LISTENING, "здоровая сессия не должна сбрасываться"


@pytest.mark.asyncio
async def test_state_changes_before_transcript_is_published():
    """Состояние обязано меняться ДО публикации события.

    `bus.publish` ждёт обработчики: если перевести сессию в THINKING
    после публикации, диалог успевает завершиться и сбросить её в IDLE,
    после чего она снова окажется в THINKING — и навсегда.
    """
    from sinaps.core.session import State as S

    seen: list[str] = []

    bus = EventBus()
    session = SessionController(bus)

    async def _transcript(event):
        # Именно это и ломало: обработчик видит IDLE вместо THINKING,
        # отрабатывает, сбрасывает сессию, а та потом снова затыкается.
        seen.append(f"видимое состояние:{session.state.value}")

    bus.on(EventType.TRANSCRIPT_FINAL)(_transcript)

    stt = FakeSTT("открой блокнот", 0.9, min_dbfs=-120.0)
    pipeline, _captured, _session = _pipeline_with_ring(_NoRing(), stt)
    pipeline.session = session
    pipeline.bus = bus
    session.state = S.TRANSCRIBING

    await pipeline._on_utterance(np.ones(SR // 2, dtype=np.float32) * 0.2)

    assert seen == ["видимое состояние:thinking"], (
        f"обработчик должен видеть THINKING, а не {seen}")


# --- эхо ассистента не должно ронять конвейер ------------------------------
#
# Регрессия из лога пользователя:
#     Фраза завершена: 0.72 с
#     Недопустимый переход idle -> transcribing
#     Контур прослушивания упал
# Микрофон слышит голос ассистента во время ответа, VAD честно находит
# фразу — но сессия уже IDLE. Переход запрещён, исключение поднималось
# до `_run`, который на любую ошибку завершался: голос мёртв до
# перезапуска программы.


class _LoopCapture:
    """Источник, который можно прокрутить N раз подряд."""

    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio
        self.sample_rate = SR
        self.frame_ms = 20
        self._ring: list[np.ndarray] = []

    async def start(self): ...
    async def stop(self): ...

    async def stream(self):
        for i in range(0, self.audio.size, 320):
            frame = self.audio[i:i + 320]
            self._ring.append(frame)
            if len(self._ring) > 25:
                self._ring.pop(0)
            yield frame

    def drain_preroll(self):
        data = (np.concatenate(self._ring) if self._ring
                else np.zeros(0, np.float32))
        self._ring.clear()
        return data


def _speech_then_silence(seconds=0.7) -> np.ndarray:
    n = int(SR * seconds)
    t = np.arange(n) / SR
    loud = (np.sin(2 * np.pi * 180 * t) * 0.3).astype(np.float32)
    return np.concatenate([loud, np.zeros(int(SR * 0.5), dtype=np.float32)])


def _echo_pipeline():
    bus = EventBus()
    session = SessionController(bus)
    stt = FakeSTT("Кто ты?", 0.8, min_dbfs=-120.0)
    pipeline, _captured, _session = _pipeline_with_ring(
        _LoopCapture(_speech_then_silence()), stt)
    pipeline.session = session
    pipeline.bus = bus
    pipeline.segmenter.vad = ScriptedVad()
    pipeline.segmenter.reset()
    return pipeline, session, stt, bus


@pytest.mark.asyncio
async def test_echo_in_idle_state_does_not_crash():
    """Голос ассистента в IDLE должен отбрасываться, а не ронять конвейер."""
    pipeline, session, stt, bus = _echo_pipeline()
    session.state = State.IDLE        # диалог уже завершён

    crashed = False
    try:
        async for frame in _LoopCapture(_speech_then_silence()).stream():
            await pipeline._on_frame(frame)
    except Exception as exc:
        crashed = True
        print(f"УПАЛО: {exc}")

    assert not crashed, "эхо не должно поднимать исключение"
    assert len(stt.calls) == 0, "распознавать эхо не нужно"
    assert session.state is State.IDLE


@pytest.mark.asyncio
async def test_real_speech_in_listening_is_recognized():
    """Настоящая речь в LISTENING должна распознаваться как обычно."""
    pipeline, session, stt, bus = _echo_pipeline()

    activated = await pipeline.activate(source="test", timeout_s=5.0)
    assert activated is True

    async for frame in _LoopCapture(_speech_then_silence()).stream():
        await pipeline._on_frame(frame)

    assert len(stt.calls) == 1, "настоящая речь должна дойти до распознавания"


@pytest.mark.asyncio
async def test_loop_survives_bad_frame():
    """Ошибка в одной фразе не должна убивать прослушивание.

    Раньше любое исключение из `_on_frame` завершало `_run`, и микрофон
    продолжал работать вхолостую, а голос не принимался. Проверяем
    именно `_run`: защита живёт внутри него, а не в `_on_frame`.
    """
    pipeline, session, stt, bus = _echo_pipeline()
    await pipeline.activate(source="test", timeout_s=5.0)

    state = {"seen": 0, "raised": False}
    original = pipeline._on_frame

    async def _explode(frame):
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("сбой в обработке кадра")
        state["seen"] += 1
        await original(frame)

    pipeline._on_frame = _explode

    # Кадры кончились — контур завершится сам, как и должен.
    await asyncio.wait_for(pipeline._run(), timeout=5.0)

    assert state["seen"] > 1, (
        "цикл должен продолжить работу после сбоя, а не оборваться")


@pytest.mark.asyncio
async def test_recovery_returns_session_to_idle():
    """После сбоя сессия обязана вернуться в IDLE."""
    pipeline, session, stt, bus = _echo_pipeline()
    session.state = State.THINKING

    pipeline._recover_after_bad_frame()

    assert session.state is State.IDLE
    assert pipeline._pending_preroll is None
    assert pipeline._turn is None


def test_session_reset_state_is_synchronous():
    """Сброс без события — чтобы восстановление не могло вызвать каскад."""
    bus = EventBus()
    session = SessionController(bus)
    session.state = State.SPEAKING
    session.pending_intent = "что-то"

    session.reset_state()

    assert session.state is State.IDLE
    assert session.pending_intent is None


# --- настройки по умолчанию ------------------------------------------------


def test_default_thresholds_are_measured_values():
    """Значения по умолчанию — не «на глаз», а из замеров."""
    cfg = SinapsConfig()
    assert cfg.stt.min_confidence == 0.25
    assert cfg.stt.min_dbfs == -50.0


def test_dbfs_matches_measured_scale():
    """Ориентиры из замеров должны оставаться верными."""
    assert dbfs(np.zeros(SR, dtype=np.float32)) == pytest.approx(-120.0)
    assert dbfs(noise(1.0, 0.001)) < -50.0      # шум комнаты
    assert dbfs(speech_like()) > -30.0          # речь