"""Тесты конвейера разговора и контура прослушивания.

Всё проверяется на заглушках: без микрофона, без моделей, без звука.
Это и есть главная ценность разделения на слои — поведение пайплайна
проверяется мгновенно.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from sinaps.audio.capture import SAMPLE_RATE
from sinaps.audio.source import FileAudioSource
from sinaps.conversation.pipeline import ConversationPipeline, Reply, make_echo_responder
from sinaps.core.bus import EventBus, EventType
from sinaps.core.errors import DangerousActionDenied, NotUnderstood, SkillError
from sinaps.core.metrics import Metrics
from sinaps.core.session import SessionController, State
from sinaps.stt.base import STTEngine, Transcript
from sinaps.tts.null_tts import NullTTS

FIXTURES = "tests/fixtures"


# --- заглушки ----------------------------------------------------------------


class FakeSTT(STTEngine):
    """Возвращает заранее заданный текст и имитирует задержку."""

    name = "fake"

    def __init__(self, text: str = "открой блокнот", delay: float = 0.0) -> None:
        super().__init__()
        self.text = text
        self.delay = delay
        self.calls = 0
        self.last_audio_len = 0

    def _load(self) -> None:
        return None

    def _recognize(self, audio: np.ndarray) -> Transcript:
        self.calls += 1
        self.last_audio_len = audio.size
        if self.delay:
            import time

            time.sleep(self.delay)
        return Transcript(text=self.text, confidence=0.95, engine=self.name)


class NullPlayer:
    """Заглушка плеера: запоминает, что «проиграли»."""

    def __init__(self) -> None:
        self.played: list[np.ndarray] = []
        self.cancelled = 0
        self.is_playing = False

    async def play(self, pcm: np.ndarray, *, sample_rate: int | None = None) -> bool:
        self.played.append(pcm)
        self.is_playing = True
        await asyncio.sleep(0)
        self.is_playing = False
        return True

    def cancel(self) -> None:
        self.cancelled += 1
        self.is_playing = False


def make_conversation(responder=None, tts: NullTTS | None = None):
    bus = EventBus()
    session = SessionController(bus)
    metrics = Metrics()
    tts = tts or NullTTS()
    player = NullPlayer()
    conv = ConversationPipeline(
        bus=bus, session=session, tts=tts, player=player, metrics=metrics,
        responder=responder or make_echo_responder(),
    )
    conv.install()
    return bus, session, metrics, conv, tts, player


# --- базовый сценарий --------------------------------------------------------


async def test_text_input_produces_reply():
    events = []

    async def collect(event):
        if event.type is EventType.RESPONSE_READY:
            events.append(event.payload["text"])

    bus, _, _, conv, tts, _ = make_conversation()
    bus.on(EventType.RESPONSE_READY)(collect)

    await conv.handle_text_input("привет")
    await asyncio.sleep(0.05)

    assert events == ["Услышал: привет"]
    assert tts.last_text == "Услышал: привет"


async def test_empty_input_is_ignored():
    _, _, _, conv, tts, _ = make_conversation()
    await conv.handle_text_input("   ")
    await asyncio.sleep(0.02)
    assert tts.calls == 0


async def test_session_returns_to_idle_after_dialogue():
    _, session, _, conv, _, _ = make_conversation()
    await conv.handle_text_input("привет")
    await asyncio.sleep(0.05)
    assert session.state is State.IDLE
    assert not session.busy


async def test_history_is_recorded():
    _, _, _, conv, _, _ = make_conversation()
    await conv.handle_text_input("первый")
    await asyncio.sleep(0.03)
    await conv.handle_text_input("второй")
    await asyncio.sleep(0.03)

    history = conv.history
    assert [h["role"] for h in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "первый"


async def test_metrics_recorded_for_each_turn():
    _, _, metrics, conv, _, _ = make_conversation()
    await conv.handle_text_input("раз")
    await asyncio.sleep(0.05)
    await conv.handle_text_input("два")
    await asyncio.sleep(0.05)
    assert metrics.stats("total")["n"] == 2


# --- обработка ошибок --------------------------------------------------------


async def test_skill_error_is_spoken_not_crashed():
    async def failing(text, ctx):
        raise SkillError("Диск повреждён", speech="Не могу открыть, диск повреждён")

    _, _, _, conv, tts, _ = make_conversation(responder=failing)
    await conv.handle_text_input("открой диск")
    await asyncio.sleep(0.05)

    assert tts.last_text == "Не могу открыть, диск повреждён"


async def test_not_understood_gives_standard_apology():
    async def not_understood(text, ctx):
        raise NotUnderstood(text)

    _, _, _, conv, tts, _ = make_conversation(responder=not_understood)
    await conv.handle_text_input("что-то странное")
    await asyncio.sleep(0.05)

    assert "попробуй" in tts.last_text.lower()


async def test_unexpected_exception_does_not_kill_pipeline():
    async def exploding(text, ctx):
        raise ZeroDivisionError("внутренняя ошибка")

    _, _, _, conv, tts, _ = make_conversation(responder=exploding)
    await conv.handle_text_input("ошибка")
    await asyncio.sleep(0.05)

    assert "не так" in tts.last_text.lower()

    # Конвейер обязан работать после сбоя.
    await conv.handle_text_input("ещё раз")
    await asyncio.sleep(0.05)
    assert tts.calls == 2


async def test_dangerous_action_denied_is_reported():
    async def dangerous(text, ctx):
        raise DangerousActionDenied()

    _, _, _, conv, tts, _ = make_conversation(responder=dangerous)
    await conv.handle_text_input("выключи компьютер")
    await asyncio.sleep(0.05)

    assert "отмен" in tts.last_text.lower()


# --- параллельные диалоги ----------------------------------------------------


async def test_concurrent_input_runs_one_dialogue():
    """Два одновременных ввода не должны ломать состояние сессии."""
    calls = []

    async def slow(text, ctx):
        calls.append(text)
        await asyncio.sleep(0.05)
        return Reply(speech="ок")

    _, session, _, conv, _, _ = make_conversation(responder=slow)
    await asyncio.gather(
        conv.handle_text_input("раз"),
        conv.handle_text_input("два"),
    )
    await asyncio.sleep(0.1)

    assert len(calls) == 1, "второй диалог должен быть отброшен, а не выполнен"
    assert session.state is State.IDLE


# --- озвучка -----------------------------------------------------------------


async def test_speak_disabled_still_produces_event():
    events = []

    async def collect(event):
        if event.type is EventType.RESPONSE_READY:
            events.append(event.payload["text"])

    bus, _, _, conv, tts, _ = make_conversation()
    bus.on(EventType.RESPONSE_READY)(collect)
    conv.set_speak_enabled(False)

    await conv.handle_text_input("тихо")
    await asyncio.sleep(0.05)

    assert events, "ответ должен попадать в UI даже без звука"
    assert tts.calls == 0, "синтез не должен вызываться при выключенной озвучке"


async def test_stop_speaking_cancels_player():
    _, _, _, conv, _, player = make_conversation()
    await conv.stop_speaking()
    assert player.cancelled == 1


# --- контур прослушивания (на файловом источнике) ---------------------------


async def test_file_source_detects_utterance_and_transcribes():
    """Полный контур на записанном файле: VAD -> фраза -> STT -> событие."""
    from sinaps.conversation.listening import ListeningPipeline

    bus = EventBus()
    session = SessionController(bus)
    metrics = Metrics()
    source = FileAudioSource(
        f"{FIXTURES}/open_notepad.wav", sample_rate=SAMPLE_RATE, frame_ms=20
    )
    stt = FakeSTT(text="открой блокнот")

    pipeline = ListeningPipeline(
        capture=source, stt=stt, vad_engine="energy", session=session,
        bus=bus, metrics=metrics, require_activation=False,
    )

    finals: list[str] = []
    starts = []

    async def on_final(event):
        if event.payload.get("text"):
            finals.append(event.payload["text"])

    async def on_start(event):
        starts.append(1)

    bus.on(EventType.TRANSCRIPT_FINAL)(on_final)
    bus.on(EventType.SPEECH_START)(on_start)

    await pipeline.start()
    await pipeline.activate(source="test", timeout_s=5.0)

    for _ in range(400):  # ждём фразу, но не вечно
        if finals:
            break
        await asyncio.sleep(0.02)

    await pipeline.stop()

    assert starts, "VAD должен заметить начало речи"
    assert finals == ["открой блокнот"], "фраза должна дойти до STT"
    assert stt.last_audio_len > SAMPLE_RATE * 0.3, "в STT ушла осмысленная длительность"


async def test_pipeline_emits_activated_event():
    from sinaps.conversation.listening import ListeningPipeline

    bus = EventBus()
    session = SessionController(bus)
    source = FileAudioSource(f"{FIXTURES}/whats_time.wav", sample_rate=SAMPLE_RATE)
    pipeline = ListeningPipeline(
        capture=source, stt=FakeSTT(), vad_engine="energy",
        session=session, bus=bus, metrics=Metrics(), require_activation=True,
    )

    activated = []

    async def watcher(event):
        activated.append(event.payload.get("source"))

    bus.on(EventType.ACTIVATED)(watcher)

    await pipeline.start()
    ok = await pipeline.activate(source="hotkey", timeout_s=2.0)
    await pipeline.stop()

    assert ok is True
    assert activated == ["hotkey"]


# --- пауза из трея ----------------------------------------------------------


def _idle_pipeline(fixture: str = "whats_time.wav"):
    """Конвейер на файле: удобно проверять, что он слушает или не слушает."""
    from sinaps.conversation.listening import ListeningPipeline

    bus = EventBus()
    session = SessionController(bus)
    source = FileAudioSource(f"{FIXTURES}/{fixture}", sample_rate=SAMPLE_RATE)
    return ListeningPipeline(
        capture=source, stt=FakeSTT(), vad_engine="energy",
        session=session, bus=bus, metrics=Metrics(), require_activation=True,
    )


async def test_pause_blocks_activation():
    """Главный смысл паузы: ассистент перестаёт реагировать на голос.

    Если после паузы он всё ещё отвечает, пользователь не может
    обсудить что-то в комнате, не боясь, что включится запись.
    """
    pipeline = _idle_pipeline()
    await pipeline.start()

    await pipeline.set_paused(True)
    assert await pipeline.activate(source="tray") is False

    await pipeline.stop()


async def test_resume_restores_activation():
    """Снятие паузы обязано возвращать работоспособность.

    Регресс на «навсегда выключенный ассистент» — состояние, из
    которого выйти нельзя, кроме перезапуска программы.
    """
    pipeline = _idle_pipeline()
    await pipeline.start()

    await pipeline.set_paused(True)
    await pipeline.set_paused(False)
    assert await pipeline.activate(source="tray") is True

    await pipeline.stop()


async def test_pause_interrupts_active_listening():
    """Пауза посреди окна прослушивания обязана его закрыть.

    Иначе микрофон продолжает писать, а индикатор показывает «Пауза».
    """
    pipeline = _idle_pipeline()
    await pipeline.start()

    assert await pipeline.activate(source="tray", timeout_s=5.0) is True
    await pipeline.set_paused(True)
    assert pipeline.session.state.value == "idle"

    await pipeline.stop()


async def test_pause_is_idempotent():
    """Повторное переключение не должно сыпать событиями."""
    pipeline = _idle_pipeline()
    await pipeline.start()

    events: list = []

    async def watcher(event):
        events.append(event.type)

    pipeline.bus.on(EventType.DEACTIVATED)(watcher)

    await pipeline.set_paused(True)
    await pipeline.set_paused(True)
    await pipeline.stop()

    assert len(events) <= 1, "пауза дважды подряд не должна деактивировать дважды"


async def test_pause_keeps_microphone_open():
    """На паузе поток микрофона остаётся открытым.

    Открывать и закрывать его дорого: при каждом переключении был бы
    слышен щелчок и задержка в полсекунды. На паузе всё равно никто
    не говорит, поэтому закрывать поток незачем.
    """
    pipeline = _idle_pipeline()
    stops: list[int] = []

    inner_stop = pipeline.capture.stop

    async def counting_stop() -> None:
        stops.append(1)
        await inner_stop()

    pipeline.capture.stop = counting_stop   # type: ignore[method-assign]

    await pipeline.start()
    await pipeline.set_paused(True)

    assert stops == [], "пауза не должна закрывать микрофон"

    await pipeline.stop()
    assert stops == [1], "а вот при выходе из программы — обязана"
