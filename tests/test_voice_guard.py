# -*- coding: utf-8 -*-
"""Голосовое подтверждение действий интерфейса.

Отзыв прав и выключение плагина — опасные действия, значит подтверждаются
так же, как выключение компьютера. Проверяется не «функция вызвалась», а
что без ответа голосом ничего не происходит: кнопка без подтверждения —
это ровно то, чего здесь добиваемся.
"""

from __future__ import annotations

import asyncio

import pytest

from sinaps.core.context import DialogContext
from sinaps.core.guard import VoiceGuard


@pytest.fixture
def guard() -> VoiceGuard:
    return VoiceGuard()


@pytest.fixture
def probe() -> DialogContext:
    """Распознаватели «да» и «нет» — те же, что у опасных команд."""
    ctx = DialogContext(text="")
    return ctx


async def answer(guard: VoiceGuard, probe: DialogContext, text: str):
    return await guard.answer(text, is_yes=probe.is_yes, is_no=probe.is_no)


# --- ожидание ответа ---------------------------------------------------------


def test_request_puts_question_in_queue(guard):
    """Вопрос виден сразу, до ответа."""
    async def action() -> str:
        return "сделано"

    assert guard.request("Отозвать права у weather?", action, "отзыв")

    assert guard.has_pending
    assert guard.question == "Отозвать права у weather?"


def test_second_request_is_refused(guard):
    """Второе действие не встаёт в очередь.

    Если бы встало, человек подтвердил бы не то, что собирался: к
    моменту ответа он помнит только первое.
    """
    async def first() -> str:
        return "первое"

    async def second() -> str:
        return "второе"

    assert guard.request("Отозвать права?", first)
    assert not guard.request("Выключить плагин?", second), (
        "второе действие встало в очередь — «да» досталось бы не тому")

    assert guard.question == "Отозвать права?"


def test_expired_request_is_not_pending(guard):
    """Просроченный вопрос не висит вечно.

    Иначе окошко в интерфейсе осталось бы с вопросом, на который уже
    никто не ответит, и следующее действие отказалось бы навсегда.
    """
    async def action() -> str:
        return "сделано"

    guard.request("Отозвать права?", action)
    guard._pending.created_at -= 1000  # noqa: SLF001 — подмена времени

    assert not guard.has_pending
    assert guard.question is None


# --- ответы ------------------------------------------------------------------


async def test_yes_executes(guard, probe):
    """«Да» выполняет действие."""
    done: list[str] = []

    async def action() -> str:
        done.append("выполнено")
        return "Права отозваны"

    guard.request("Отозвать права у weather?", action)
    executed, message = await answer(guard, probe, "да")

    assert executed is True
    assert done == ["выполнено"]
    assert message == "Права отозваны"
    assert not guard.has_pending


async def test_no_cancels(guard, probe):
    """«Нет» не выполняет ничего."""
    done: list[str] = []

    async def action() -> str:
        done.append("выполнено")
        return "сделано"

    guard.request("Отозвать права у weather?", action)
    executed, message = await answer(guard, probe, "нет")

    assert executed is False
    assert done == [], "действие выполнилось вопреки «нет»"
    assert "отменяю" in message.lower()


async def test_unclear_answer_cancels(guard, probe):
    """Неясный ответ отменяет, а не ждёт дальше.

    Ожидание продолжения означало бы, что «подожди» когда-нибудь
    превратится в «да» и действие выполнится мимо воли человека.
    """
    done: list[str] = []

    async def action() -> str:
        done.append("выполнено")
        return "сделано"

    guard.request("Отозвать права у weather?", action)
    executed, message = await answer(guard, probe, "а что если")

    assert executed is False
    assert done == []
    assert "не понял" in message.lower()


async def test_expired_answer_cancels(guard, probe):
    """Ответ после срока отменяет действие."""
    done: list[str] = []

    async def action() -> str:
        done.append("выполнено")
        return "сделано"

    guard.request("Отозвать права?", action)
    guard._pending.created_at -= 1000  # noqa: SLF001 — подмена времени

    executed, message = await answer(guard, probe, "да")

    assert executed is False
    assert done == [], "просроченное подтверждение выполнило действие"
    assert "время" in message.lower()


async def test_answer_without_question_is_harmless(guard, probe):
    """Ответ без вопроса не ломает разговор.

    Человек сказал «да» ни к чему — это обычная реплика, и движок
    понимания должен обработать её как обычно, а не падать.
    """
    executed, message = await answer(guard, probe, "да")

    assert executed is False
    assert message == "Нечего подтверждать"


async def test_question_used_once(guard, probe):
    """Вопрос одноразовый.

    Второе «да» вслед за первым не должно выполнить действие снова:
    человек подтвердил отзыв один раз, а не два.
    """
    done: list[str] = []

    async def action() -> str:
        done.append("выполнено")
        return "сделано"

    guard.request("Отозвать права?", action)
    await answer(guard, probe, "да")
    executed, _ = await answer(guard, probe, "да")

    assert done == ["выполнено"], "действие выполнилось второй раз"
    assert executed is False


# --- сбои --------------------------------------------------------------------


async def test_failing_action_reports_honestly(guard, probe):
    """Сбой подтверждённого действия не выдаётся за успех.

    Подтверждение получено, а действие не вышло. Сказать «готово» —
    значит человек будет ждать результата, которого не будет.
    """
    async def action() -> str:
        raise OSError("файл занят")

    guard.request("Отозвать права?", action)
    executed, message = await answer(guard, probe, "да")

    assert executed is False
    assert "не получилось" in message.lower()


async def test_cancel_drops_question(guard):
    """Остановка ассистента снимает вопрос.

    Ответ при следующем запуске выполнил бы действие, о котором человек
    уже забыл.
    """
    async def action() -> str:
        return "сделано"

    guard.request("Отозвать права у weather?", action)
    guard.cancel()

    assert not guard.has_pending
    assert guard.question is None


async def test_slow_action_does_not_block_answer(guard, probe):
    """Медленное действие не мешает следующим ответам.

    Вопрос снимается **до** выполнения, иначе второе действие
    отказалось бы на всё время первой перезагрузки.
    """
    async def slow() -> str:
        await asyncio.sleep(0.01)
        return "сделано"

    async def other() -> str:
        return "другое"

    guard.request("Отозвать права?", slow)
    task = asyncio.create_task(answer(guard, probe, "да"))

    # Даём задаче дойти до `action`: вопрос должен сняться до
    # выполнения, а не после. Без `await asyncio.sleep(0)` задача
    # ещё не стартовала, и проверка была бы на пустом месте.
    await asyncio.sleep(0)

    assert not guard.has_pending, "вопрос держался до конца действия"
    assert guard.request("Выключить плагин?", other), (
        "следующее действие отказалось из-за предыдущего")

    assert (await task)[0] is True