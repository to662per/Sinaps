"""Асинхронная шина событий — единственный способ общения между модулями.

Почему шина, а не прямые вызовы:
  * аудио-поток, STT, TTS и навыки живут в разных задачах;
  * ядро не должно знать, кто именно на него подписан;
  * так жестко проверяется правило зависимостей (см. tests/test_architecture.py).

Подписчики одного события выполняются параллельно; исключение в одном
подписчике не роняет остальных и не убивает шину.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


class EventType(str, enum.Enum):
    """Типы событий. Значение — стабильный ключ для логов и тестов."""

    # Жизненный цикл сессии
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    STATE_CHANGED = "state_changed"
    ACTIVATED = "activated"        # окно прослушивания открыто (хоткей/ptt/wake)
    DEACTIVATED = "deactivated"    # окно закрыто пользователем или таймаутом

    # Аудио
    AUDIO_STARTED = "audio_started"
    AUDIO_STOPPED = "audio_stopped"
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"

    # Речь
    TRANSCRIPT_PARTIAL = "transcript_partial"
    TRANSCRIPT_FINAL = "transcript_final"

    # Понимание
    INTENT_RESOLVED = "intent_resolved"
    SLOTS_FILLED = "slots_filled"
    NEED_CLARIFY = "need_clarify"

    # Действие
    SKILL_STARTED = "skill_started"
    SKILL_FINISHED = "skill_finished"
    CONFIRM_REQUESTED = "confirm_requested"

    # Ответ
    RESPONSE_READY = "response_ready"
    TTS_STARTED = "tts_started"
    TTS_FINISHED = "tts_finished"
    BARGE_IN = "barge_in"

    # Служебное
    ERROR = "error"
    METRIC = "metric"


@dataclass(frozen=True)
class Event:
    """Событие. `payload` — простой dict, чтобы его легко было логировать."""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.monotonic)

    def __repr__(self) -> str:  # компактнее в логах, чем дефолтный repr dataclass
        return f"Event({self.type.value}, {self.payload})"


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[EventType, list[Handler]] = defaultdict(list)
        self._history: list[Event] = []
        self._keep_history = 200

    # --- подписка ----------------------------------------------------------

    def on(self, *event_types: EventType) -> Callable[[Handler], Handler]:
        """Декоратор: `@bus.on(EventType.TRANSCRIPT_FINAL, EventType.WAKE)`.

        Одна и та же функция может подписаться на несколько событий.
        """

        def deco(fn: Handler) -> Handler:
            for et in event_types:
                self._subs[et].append(fn)
            return fn

        return deco

    def off(self, event_type: EventType, fn: Handler) -> None:
        try:
            self._subs[event_type].remove(fn)
        except ValueError:
            pass

    # --- публикация -------------------------------------------------------

    async def publish(self, event: Event) -> None:
        self._history.append(event)
        if len(self._history) > self._keep_history:
            del self._history[: len(self._history) - self._keep_history]

        handlers = self._subs.get(event.type)
        if not handlers:
            return

        results = await asyncio.gather(
            *(self._call(h, event) for h in handlers),
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                log.error("Подписчик %s упал на %s: %r", _name(res), event.type, res)

    async def emit(self, event_type: EventType, **payload: Any) -> None:
        """Короткая форма: `await bus.emit(EventType.WAKE, source="hotkey")`."""
        await self.publish(Event(type=event_type, payload=payload))

    @staticmethod
    async def _call(handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Подписчик %s упал на %s", _name(handler), event.type)

    # --- отладка -----------------------------------------------------------

    def recent(self, n: int = 20, *types: EventType) -> list[Event]:
        out = self._history[-n:]
        if types:
            out = [e for e in out if e.type in types]
        return out

    def clear_history(self) -> None:
        self._history.clear()


def _name(obj: object) -> str:
    return getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None) or repr(obj)
