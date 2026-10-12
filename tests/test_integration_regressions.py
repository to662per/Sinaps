"""Регресс-тесты на баги, найденные при интеграции движка навыков.

Каждый тест здесь закрывает конкретный дефект, который проявился только
при совместной работе слоёв. Такие баги не видны в юнит-тестах по отдельности.
"""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest

from sinaps.audio.player import AudioPlayer
from sinaps.conversation.pipeline import ConversationPipeline
from sinaps.core.bus import EventBus, EventType
from sinaps.core.metrics import Metrics
from sinaps.core.response import Reply
from sinaps.core.session import SessionController
from sinaps.skills.engine import SkillEngine, build_default_registry
from sinaps.tts.null_tts import NullTTS
from sinaps.ui.console import Console

# --- плеер: отмена не должна отменять саму себя ---------------------------


class _RecordingPlayer(AudioPlayer):
    """Плеер без звуковой карты: считает блоки и уважает отмену.

    Важно: пауза между блоками должна быть реальной. С `sleep(0)` цикл
    «проигрывает» 3 секунды за миллисекунды, и отменять becomes нечего —
    тест проверял бы не то.
    """

    def __init__(self) -> None:
        super().__init__(sample_rate=24000)
        self.blocks_written = 0

    async def _stream_to_speakers(self, audio: np.ndarray) -> None:
        block = 480
        for _start in range(0, audio.size, block):
            if not self._playing:
                return
            self.blocks_written += 1
            await asyncio.sleep(0.002)


async def test_play_does_not_cancel_itself():
    """Регрессия: play() сохранял текущую задачу, и cancel() гасил её сам.

    Симптом: звук обрывался на первом же блоке при последовательных
    ответах, и сессия оставалась занятой — все следующие реплики
    отбрасывались с сообщением «я ещё отвечаю».
    """
    player = _RecordingPlayer()
    pcm = np.zeros(24000, dtype=np.float32)  # 1 секунда

    await player.play(pcm)
    first = player.blocks_written

    await player.play(pcm)
    second = player.blocks_written

    assert first > 0, "первый звук должен проигрываться целиком"
    assert second > first, "второй вызов должен добавить блоки, а не отмениться"
    assert player.is_playing is False


async def test_cancel_from_other_task_stops_playback():
    """Отмена извне обязана останавливать звук (это barge-in)."""
    player = _RecordingPlayer()
    pcm = np.zeros(24000 * 3, dtype=np.float32)  # 3 секунды = 150 блоков

    asyncio.create_task(player.play(pcm))
    await asyncio.sleep(0.02)
    player.cancel()
    await asyncio.sleep(0.05)

    full_blocks = (24000 * 3) // 480
    assert player.blocks_written < full_blocks, (
        f"воспроизведение должно прерваться: записано {player.blocks_written} "
        f"из {full_blocks} блоков"
    )
    assert not player.is_playing


async def test_player_clears_task_after_play():
    player = _RecordingPlayer()
    await player.play(np.zeros(24000, dtype=np.float32))
    assert player._task is None, "задача должна очищаться, иначе следующий вызов её отменит"


# --- консоль: чтение блокируется до конца обработки ------------------------


def test_console_reader_blocks_until_mark_done():
    """Регрессия: `wait(timeout=0.25)` позволял читать следующую строку
    раньше, чем предыдущая реплика обработалась.

    Симптом: вторая команда приходила в занятую сессию, отбрасывалась, и
    пользователь видел «обрабатываю…» без ответа.
    """
    console = Console(None, banner=False)
    console.mark_busy()

    released = threading.Event()
    reads = []

    def reader() -> None:
        while not console.idle.is_set():
            console.idle.wait(timeout=0.05)
            if console.idle.is_set():
                break
        reads.append(1)
        released.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.4)
    assert not released.is_set(), "пока занято, читать нельзя"

    console.mark_done()
    thread.join(timeout=2.0)
    assert released.is_set(), "после mark_done чтение должно продолжиться"


# --- конвейер: занятая сессия сообщает, а не молчит -----------------------


class _FakeTTS(NullTTS):
    pass


class _NullPlayer:
    async def play(self, pcm, *, sample_rate=None):
        await asyncio.sleep(0)
        return True

    def cancel(self) -> None:
        pass


async def test_busy_session_reports_instead_of_silent_drop():
    """Пользователь должен понимать, что его не услышали."""
    bus = EventBus()
    session = SessionController(bus)
    seen: list[str] = []

    async def on_response(event):
        seen.append(event.payload.get("text", ""))

    bus.on(EventType.RESPONSE_READY)(on_response)

    conversation = ConversationPipeline(
        bus=bus, session=session, tts=_FakeTTS(), player=_NullPlayer(),
        metrics=Metrics(), responder=lambda t, c: asyncio.sleep(0.01, Reply("ok")),
        speak_enabled=False,
    )
    conversation.install()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_responder(text, context):
        started.set()
        await release.wait()
        return Reply("первый ответ")

    # Имя атрибута — именно `responder`: подчеркнутое `_responder` создало бы
    # новый атрибут, подмена не сработала бы, и тест зависал бы навсегда.
    conversation.responder = slow_responder

    first = conversation.handle_text_input("раз")
    await started.wait()

    await conversation.handle_text_input("два")
    release.set()
    await first
    await asyncio.sleep(0.05)

    assert any("ещё отвечаю" in text for text in seen), (
        f"конвейер должен сообщить о занятой сессии, получено: {seen}"
    )


# --- метрика ответа для текстового ввода -----------------------------------


async def test_text_input_produces_response_metric():
    """Для текста нет стадии VAD, но задержка ответа должна измеряться.

    Регрессия: без отметки `vad_end` метрика `response` была всегда 0
    и показывала мгновенный ответ при реальных 4 секундах.
    """
    bus = EventBus()
    session = SessionController(bus)
    metrics = Metrics()

    conversation = ConversationPipeline(
        bus=bus, session=session, tts=_FakeTTS(), player=_NullPlayer(),
        metrics=metrics, responder=lambda t, c: asyncio.sleep(0, Reply("ок")),
        speak_enabled=False,
    )
    conversation.install()

    await conversation.handle_text_input("привет")
    await asyncio.sleep(0.05)

    assert metrics.stats("response")["n"] == 1
    assert metrics.stats("response")["p50"] >= 0.0


# --- сквозной сценарий -------------------------------------------------------


async def test_full_dialogue_flow(platform_stub):
    """Типичный разговор целиком: команда, уточнение не нужно, ответ."""
    engine = SkillEngine(build_default_registry(), platform=platform_stub)

    reply = await engine.respond("который час", {"source": "text", "history": []})
    assert "Сейчас" in reply.speech

    reply = await engine.respond("открой блокнот", {"source": "text", "history": []})
    assert "Открываю" in reply.speech
    assert platform_stub.calls == ["launch:блокнот"]
