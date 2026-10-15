# -*- coding: utf-8 -*-
"""Подтверждение голосом проходит через обычный разговор.

Отдельно от `VoiceGuard` проверяется тут одно: реплика «да» на вопрос
интерфейса доходит до ответа, а не падает в разбор команд. Если это
сломается, человек услышит «не знаю такую команду» вместо ответа на
вопрос про отзыв прав — и решит, что сломалось подтверждение.
"""

from __future__ import annotations

import asyncio

import pytest

from sinaps.core.bus import EventBus, EventType
from sinaps.core.context import DialogContext
from sinaps.core.guard import VoiceGuard
from sinaps.core.metrics import Metrics
from sinaps.core.response import Reply
from sinaps.core.session import SessionController, State
from sinaps.conversation.pipeline import ConversationPipeline


class FakePlayer:
    """Проигрыватель без звука."""

    def __init__(self) -> None:
        self.cancelled = 0

    def play(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def cancel(self) -> None:
        self.cancelled += 1


class FakeTTS:
    """Синтез без звука: возвращает, что должен был сказать."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def speak(self, text: str, player=None) -> bool:  # noqa: ANN001
        # Позиционный `player` обязателен: конвейер зовёт
        # `tts.speak(text, self.player)`.
        self.spoken.append(text)
        return True

    async def stop(self) -> None:
        pass


class RecordingResponder:
    """Разбор команд: запоминает, что до него дошло."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, text: str, context: dict) -> Reply:
        self.seen.append(text)
        return Reply(speech=f"разобрано: {text}")


@pytest.fixture
def pipeline():
    bus = EventBus()
    guard = VoiceGuard()
    session = SessionController(bus=bus)
    responder = RecordingResponder()
    tts = FakeTTS()

    pipe = ConversationPipeline(
        bus=bus, session=session, tts=tts, player=FakePlayer(),
        metrics=Metrics(), responder=responder, guard=guard,
    )
    return pipe, responder, tts, guard, session


async def say(pipe, text: str) -> None:
    await pipe.handle_text_input(text)
    await asyncio.sleep(0)
    # Дать дойти вложенным задачам озвучки.
    for _ in range(3):
        await asyncio.sleep(0)


# --- ответ на вопрос ---------------------------------------------------------


async def test_yes_does_not_reach_the_matcher(pipeline):
    """«Да» на вопрос не попадает в разбор команд.

    Движок не знает ни слова про отзыв прав: без этой проверки человек
    услышал бы «не знаю такую команду» вместо выполнения.
    """
    pipe, responder, tts, guard, _ = pipeline
    done: list[str] = []

    async def action() -> str:
        done.append("выполнено")
        return "Права отозваны"

    guard.request("Отозвать права у weather?", action)

    await say(pipe, "да")

    assert responder.seen == [], (
        f"«да» ушло в разбор команд: {responder.seen}")
    assert done == ["выполнено"]
    assert "Права отозваны" in tts.spoken[-1]


async def test_no_reaches_the_action_denied(pipeline):
    """«Нет» отменяет действие."""
    pipe, responder, tts, guard, _ = pipeline
    done: list[str] = []

    async def action() -> str:
        done.append("выполнено")
        return "сделано"

    guard.request("Отозвать права у weather?", action)

    await say(pipe, "нет")

    assert done == [], "действие выполнилось вопреки «нет»"
    assert responder.seen == []
    assert any("отменяю" in line.lower() for line in tts.spoken)


async def test_answer_while_listening_works(pipeline):
    """Ответ голосом во время прослушивания не роняет диалог.

    Именно так выглядит живой случай: человек нажал кнопку в трее, а
    ассистент спросил подтверждение голосом. Сессия в этот момент в
    состоянии LISTENING, а не IDLE, и переход `listening -> thinking`
    был запрещён таблицей. Диалог падал целиком, и в историю не
    попадало ничего — на вопрос «очистить историю» человек отвечал
    «да», ассистент говорил «Что-то пошло не так», и история оставалась
    пустой.

    Тест повторяет именно этот порядок состояний.
    """
    pipe, responder, tts, guard, session = pipeline
    done: list[str] = []

    async def action() -> str:
        done.append("выполнено")
        return "История очищена"

    # Сессия жива: окно прослушивания открыто.
    await session.transition(State.LISTENING, reason="test")

    guard.request("Очистить всю историю?", action)
    await say(pipe, "да")

    assert done == ["выполнено"], (
        f"действие не выполнено: состояние {session.state.value}, "
        f"сказано: {tts.spoken}")
    assert "История очищена" in tts.spoken[-1]
    assert responder.seen == [], "«да» ушло в разбор команд"


async def test_answer_while_listening_reaches_history(pipeline):
    """Обмен с подтверждением попадает в историю.

    Отдельная проверка от предыдущей: `_remember` вызывается до озвучки,
    и падение на переходе теряло не только ответ, но и запись.
    """
    pipe, _responder, _tts, guard, session = pipeline
    written: list[tuple] = []

    class _Memory:
        def add_turn(self, user_text, bot_text, **kw):
            written.append((user_text, bot_text))
            return 1

        def add_dialogue(self, role, text, **kw):
            written.append((role, text))
            return 1

    pipe.memory = _Memory()

    async def action() -> str:
        return "История очищена"

    await session.transition(State.LISTENING, reason="test")
    guard.request("Очистить всю историю?", action)
    await say(pipe, "да")

    assert written, "обмен с подтверждением не записан в историю"
    assert any("да" == str(row[1]) for row in written), written


async def test_normal_command_while_listening_works(pipeline):
    """Обычная команда во время прослушивания тоже работает.

    Тот же маршрут состояний, но без вопроса: если сломать переход,
    пострадал бы весь голосовой ввод, а не только подтверждения.
    """
    pipe, responder, tts, _guard, session = pipeline

    await session.transition(State.LISTENING, reason="test")
    await say(pipe, "открой блокнот")

    assert responder.seen, "реплика не дошла до разбора"
    assert tts.spoken, "ассистент не ответил"


async def test_regular_rephrase_without_question(pipeline):
    """Без вопроса реплика разбирается как обычно.

    Иначе после отмены любой команды движок молчал бы: guard остался бы
    в состоянии ожидания навсегда.
    """
    pipe, responder, _tts, guard, _ = pipeline

    guard.request("Отозвать права у weather?", _noop)
    await say(pipe, "нет")
    await say(pipe, "открой блокнот")

    assert responder.seen == ["открой блокнот"]


async def test_guard_answer_is_logged(pipeline, caplog):
    """Ответ на вопрос попадает в журнал разговора.

    Отдельной строкой: по общему виду реплик не отличить «да» в ответ на
    отзыв прав от «да» в пустоту.
    """
    pipe, _responder, _tts, guard, _ = pipeline
    guard.request("Отозвать права у weather?", _noop)

    with caplog.at_level("INFO"):
        await say(pipe, "нет")

    assert any("ПОДТВ" in r.message or "отменяю" in r.message.lower()
               for r in caplog.records), (
        "ответ на подтверждение не попал в журнал")


async def test_busy_answers_whole_dialogue(pipeline):
    """Сессия освобождается после ответа на вопрос.

    Иначе второй вопрос остался бы без ответа, а сессия — занятой до
    конца сеанса.
    """
    pipe, _responder, _tts, guard, session = pipeline
    guard.request("Отозвать права у weather?", _noop)

    await say(pipe, "нет")

    assert not session.busy, "сессия осталась занятой"
    assert session.state is State.IDLE


async def test_conversation_survives_failing_guard_action(pipeline):
    """Сбой подтверждённого действия не роняет разговор."""
    pipe, _responder, tts, guard, session = pipeline

    async def broken() -> str:
        raise OSError("файл занят")

    guard.request("Отозвать права у weather?", broken)

    await say(pipe, "да")

    assert not session.busy
    assert any("не получилось" in line.lower() for line in tts.spoken), (
        f"сбой не озвучен: {tts.spoken}")
    # И разговор продолжается.
    await say(pipe, "открой блокнот")


async def _noop() -> str:
    return "сделано"