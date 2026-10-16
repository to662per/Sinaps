"""Конечный автомат сессии.

Один автомат на одну речь пользователя. Он решает три задачи:

1. Не дать двум диалогам пересечься (двойное нажатие хоткея, wake во время
   ответа). Переходы идут только через `transition()`, поэтому гонок нет.
2. Дать точку для barge-in: во время SPEAKING речь пользователя глушит TTS.
3. Дать таймауты: LISTENING без речи и THINKING без ответа не должны
   «залипать» навсегда.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time

from .bus import EventBus, EventType

log = logging.getLogger(__name__)


class State(str, enum.Enum):
    IDLE = "idle"                    # спим, слушаем только wake-word
    LISTENING = "listening"          # идёт запись речи
    TRANSCRIBING = "transcribing"    # распознаём
    THINKING = "thinking"            # понимаем намерение
    ROUTING = "routing"              # выполняем навык
    SPEAKING = "speaking"            # озвучиваем ответ
    CONFIRMING = "confirming"        # ждём «да/нет» на опасное действие
    ERROR = "error"

    @property
    def is_busy(self) -> bool:
        """True, если идёт активный диалог (нельзя начинать новый)."""
        return self not in (State.IDLE,)


#: Разрешённые переходы. Всё остальное — ошибка программиста, а не пользователя.
#:
#: Обратите внимание на IDLE -> THINKING: это путь для реплики, введённой
#: текстом (консоль, трей). Акустических стадий LISTENING/TRANSCRIBING у неё
#: нет, поэтому заставлять её «притворяться» голосом было бы artificial.
_ALLOWED: dict[State, set[State]] = {
    State.IDLE: {State.LISTENING, State.THINKING,
                      # Ассистент заговаривает сам, без реплики:
                      # «Прослушать пример» в настройках, сработавший
                      # таймер. Из IDLE это законный переход, и без него
                      # фраза не произносится вовсе.
                      State.SPEAKING},
    # LISTENING -> THINKING добавлен для ответа на вопрос подтверждения:
    # вопрос задаётся голосом, человек отвечает голосом прямо во время
    # прослушивания, и конвейер обрабатывает реплику из LISTENING, а не
    # из IDLE. Без этого перехода весь диалог падал с InvalidTransition,
    # и в историю не попадало ничего: человек подтверждал отзыв прав,
    # ассистент отвечал «Что-то пошло не так», и история оставалась
    # пустой. Тест на подтверждение это скрывал: сессия в нём всегда
    # начиналась из IDLE.
    State.LISTENING: {State.TRANSCRIBING, State.THINKING, State.IDLE,
                      State.SPEAKING, State.ERROR},
    State.TRANSCRIBING: {State.THINKING, State.IDLE, State.ERROR},
    State.THINKING: {State.ROUTING, State.LISTENING, State.SPEAKING, State.ERROR},
    State.ROUTING: {State.SPEAKING, State.CONFIRMING, State.LISTENING, State.IDLE, State.ERROR},
    State.SPEAKING: {State.IDLE, State.LISTENING, State.ERROR},
    State.CONFIRMING: {State.ROUTING, State.LISTENING, State.IDLE, State.ERROR},
    State.ERROR: {State.IDLE, State.LISTENING, State.SPEAKING},
}


class InvalidTransition(RuntimeError):
    def __init__(self, src: State, dst: State) -> None:
        super().__init__(f"Недопустимый переход {src.value} -> {dst.value}")
        self.src, self.dst = src, dst


class SessionController:
    """Сериализует работу с состоянием сессии.

    `Lock` вокруг переходов не нужен: сам event loop однопоточный, а все
    переходы идут из корутин. `Lock` нужен только для `run_dialogue`, чтобы
    гарантировать, что одновременно выполняется один диалог.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.state = State.IDLE
        self._dialogue_lock = asyncio.Lock()
        self._since = time.monotonic()
        self.pending_intent: str | None = None   # ждём уточняющий ответ

    # --- переходы ----------------------------------------------------------

    async def transition(self, new_state: State, *, reason: str = "", **payload) -> None:
        old = self.state
        if new_state is not old and new_state not in _ALLOWED[old]:
            log.warning("Недопустимый переход %s -> %s (%s)", old.value, new_state.value, reason)
            raise InvalidTransition(old, new_state)
        self.state = new_state
        self._since = time.monotonic()
        log.debug("Состояние: %s -> %s (%s)", old.value, new_state.value, reason)
        await self._bus.emit(EventType.STATE_CHANGED, old=old.value, new=new_state.value,
                             reason=reason, **payload)

    async def reset(self, *, reason: str = "reset") -> None:
        """Вернуться в IDLE из любого состояния (иначе, таймаут, отмена)."""
        self.reset_state()
        log.info("Сессия сброшена: %s", reason)
        await self._bus.emit(EventType.STATE_CHANGED, old="*", new=State.IDLE.value, reason=reason)

    def reset_state(self) -> None:
        """Сброс без публикации события.

        Нужен при аварийном восстановлении внутри горячего пути: там
        нельзя ждать публикацию, потому что она сама может разбудить
        подписчиков и вызвать повторный сбой.
        """
        self.state = State.IDLE
        self.pending_intent = None
        self._since = time.monotonic()

    # --- выполнение диалога ------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.state.is_busy

    async def try_acquire(self) -> bool:
        """Занять сессию. False — диалог уже идёт, второй надо отбросить."""
        if self._dialogue_lock.locked():
            return False
        await self._dialogue_lock.acquire()
        return True

    def release(self) -> None:
        if self._dialogue_lock.locked():
            self._dialogue_lock.release()

    @property
    def elapsed_in_state(self) -> float:
        return time.monotonic() - self._since

    @property
    def pending(self) -> str | None:
        return self.pending_intent

    @pending.setter
    def pending(self, intent: str | None) -> None:
        self.pending_intent = intent
