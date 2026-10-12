"""Тесты ядра: шина событий, FSM, метрики, конфиг.

Эти тесты не требуют микрофона, моделей и сети — они должны проходить
на любой машине за доли секунды.
"""

from __future__ import annotations

import asyncio

import pytest

from sinaps.core.bus import Event, EventBus, EventType
from sinaps.core.config import SinapsConfig
from sinaps.core.metrics import Metrics
from sinaps.core.session import InvalidTransition, SessionController, State


# --- шина событий -----------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_delivers_to_subscriber():
    bus = EventBus()
    got = []

    @bus.on(EventType.RESPONSE_READY)
    async def handler(event):
        got.append(event.payload["text"])

    await bus.emit(EventType.RESPONSE_READY, text="привет")
    assert got == ["привет"]


@pytest.mark.asyncio
async def test_bus_subscriber_isolated_from_failure():
    """Падение одного подписчика не должно ломать остальных и шину."""
    bus = EventBus()
    survived = []

    @bus.on(EventType.ERROR)
    async def bad(event):
        raise RuntimeError("подписчик сломался")

    @bus.on(EventType.ERROR)
    async def good(event):
        survived.append(1)

    await bus.emit(EventType.ERROR, detail="x")
    assert survived == [1]

    # Шина продолжает работать после исключения.
    await bus.emit(EventType.ERROR, detail="y")
    assert survived == [1, 1]


@pytest.mark.asyncio
async def test_bus_runs_subscribers_concurrently():
    """Два подписчика должны стартовать вместе, а не по очереди."""
    bus = EventBus()
    order = []

    async def slow(event):
        await asyncio.sleep(0.05)
        order.append("slow")

    async def fast(event):
        order.append("fast")

    bus.on(EventType.SPEECH_START)(slow)
    bus.on(EventType.SPEECH_START)(fast)

    await bus.emit(EventType.SPEECH_START)
    assert order == ["fast", "slow"]


def test_bus_event_repr_is_readable():
    event = Event(EventType.TRANSCRIPT_FINAL, {"text": "открой блокнот"})
    assert "TRANSCRIPT_FINAL" in repr(event) or "transcript_final" in repr(event)


@pytest.mark.asyncio
async def test_bus_recent_filters_by_type():
    bus = EventBus()
    await bus.publish(Event(EventType.SPEECH_START, {}))
    await bus.publish(Event(EventType.SPEECH_END, {}))
    only_ends = bus.recent(10, EventType.SPEECH_END)
    assert len(only_ends) == 1


# --- конечный автомат -------------------------------------------------------


@pytest.mark.asyncio
async def test_session_rejects_illegal_transition():
    session = SessionController(EventBus())
    with pytest.raises(InvalidTransition):
        await session.transition(State.SPEAKING, reason="из IDLE нельзя")


@pytest.mark.asyncio
async def test_session_allows_legal_chain():
    bus = EventBus()
    session = SessionController(bus)
    seen = []

    @bus.on(EventType.STATE_CHANGED)
    async def watcher(event):
        seen.append(event.payload["new"])

    await session.transition(State.LISTENING, reason="test")
    await session.transition(State.TRANSCRIBING, reason="test")
    await session.transition(State.THINKING, reason="test")
    await session.transition(State.ROUTING, reason="test")
    await session.transition(State.SPEAKING, reason="test")
    await session.transition(State.IDLE, reason="test")

    assert seen == ["listening", "transcribing", "thinking", "routing", "speaking", "idle"]
    assert session.state is State.IDLE


@pytest.mark.asyncio
async def test_session_reset_from_any_state():
    session = SessionController(EventBus())
    await session.transition(State.LISTENING, reason="t")
    await session.reset(reason="пользователь отменил")
    assert session.state is State.IDLE


@pytest.mark.asyncio
async def test_session_lock_allows_only_one_dialogue():
    session = SessionController(EventBus())
    assert await session.try_acquire() is True
    assert await session.try_acquire() is False
    session.release()
    assert await session.try_acquire() is True


def test_state_is_busy():
    assert State.IDLE.is_busy is False
    assert State.LISTENING.is_busy is True
    assert State.SPEAKING.is_busy is True


# --- метрики ----------------------------------------------------------------


def test_metrics_percentiles():
    m = Metrics(window=100)
    for _ in range(10):
        turn = m.new_turn()
        turn.mark("vad_end")
        turn.mark("stt_done")
        turn.mark("first_audio")
        m.finish(turn)

    stats = m.stats("total")
    assert stats["n"] == 10
    assert stats["p50"] >= 0
    assert "p95" in stats and "mean" in stats and "max" in stats


def test_metrics_report_contains_stages():
    m = Metrics()
    turn = m.new_turn()
    turn.mark("vad_end")
    m.finish(turn)
    report = m.report()
    assert "вad_end" in report or "vad_end" in report
    assert "p50" in report


def test_metrics_pass_rate_threshold():
    m = Metrics()
    for _ in range(10):
        turn = m.new_turn()
        turn.mark("first_audio")
        m.finish(turn)
    # Первый диалог ~0 с, значит почти все быстрее порога.
    assert m.pass_rate("response", threshold=1.6) >= 0.9


def test_response_time_excludes_speech_duration():
    """Ответ = конец фразы -> звук. Длительность речи в неё не входит."""
    import time

    turn = Metrics().new_turn()
    time.sleep(0.02)     # пользователь говорил
    turn.mark("vad_end")
    time.sleep(0.01)     # система отвечает
    turn.mark("first_audio")

    assert turn.response_time == pytest.approx(0.01, abs=0.02)
    assert turn.total > turn.response_time


def test_response_time_zero_when_stages_missing():
    turn = Metrics().new_turn()
    assert turn.response_time == 0.0


def test_metrics_deltas_are_ordered():
    m = Metrics()
    turn = m.new_turn()
    turn.mark("vad_end")
    turn.mark("stt_done")
    turn.mark("intent")
    deltas = turn.deltas()
    assert list(deltas) == ["vad_end", "stt_done", "intent"]
    assert deltas["vad_end"] >= 0


# --- конфигурация -----------------------------------------------------------


def test_config_defaults_are_valid():
    cfg = SinapsConfig()
    assert cfg.validate() == []


def test_config_rejects_bad_values():
    cfg = SinapsConfig()
    cfg.tts.speed = 5.0  # вне диапазона 0.5..2.0
    with pytest.raises(Exception):
        cfg.validate()


def test_config_roundtrip():
    cfg = SinapsConfig()
    cfg.stt.model = "medium"
    cfg.tts.voice = "aidar"
    cfg.privacy.local_only = True

    text = cfg.dumps()
    assert "medium" in text and "aidar" in text

    restored = SinapsConfig.from_dict(cfg.to_dict())
    assert restored.stt.model == "medium"
    assert restored.tts.voice == "aidar"
    assert restored.privacy.local_only is True


def test_config_ignores_unknown_keys():
    restored = SinapsConfig.from_dict({"stt": {"model": "base"}, "unknown_section": {}})
    assert restored.stt.model == "base"


def test_config_local_only_is_default():
    """Требование проекта: по умолчанию сеть запрещена."""
    assert SinapsConfig().privacy.local_only is True


def test_config_yaml_is_readable():
    text = SinapsConfig().dumps()
    assert "stt:" in text and "tts:" in text and "vad:" in text
