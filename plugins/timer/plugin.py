# -*- coding: utf-8 -*-
"""Таймер: «через пять минут напомни».

Срок ставит ядро (`sinaps/core/scheduler.py`), а не сам плагин.
Разница видна на перезапуске: собственная задача asyncio умирает вместе
с процессом, и человек, вернувшись, не услышал бы ничего — молчание
выглядит как «напоминания не работают». Ядро хранит сроки в файле и
поднимает их при следующем запуске, а просроченное срабатывает сразу.

Право `schedule` проверяется ядром до записи. Плагин без него получает
отказ, а не срок, который не сработает.

Что здесь нет намеренно: повторяющихся задач («каждый день в девять»).
Поле для них в таблице сроков есть, но обещать их, пока они не
сделаны, нельзя.
"""

from __future__ import annotations

import re

from sinaps.skills.base import Response

#: Один таймер на плагин. Несколько понадобятся заметкам, но там
#: каждый — свой, а здесь человек просит один.
PLAN_ID = "timer-1"

# Разбор времени и формы слов — в ядре (`sinaps/core/when.py`). Здесь
# только тонкие обёртки: логика разбора нужна и таймеру, и
# напоминанию, и две копии разъедутся за месяц.
from sinaps.core.when import human_duration, parse_duration


def _seconds(text: str) -> int | None:
    """Сколько секунд просил человек. `None` — не понял."""
    return parse_duration(text)


def _human(seconds: int) -> str:
    """Сколько осталось — словами и с правильными формами."""
    return human_duration(seconds)


async def _start(ctx, slots) -> Response:
    """Поставить таймер через расписание ядра."""
    seconds = _seconds((slots or {}).get("amount") or "")
    if seconds is None:
        return Response(speech=("Не понял, на сколько. Скажите: «поставь "
                                "таймер на пять минут»."))
    await ctx.schedule(PLAN_ID, seconds, {"why": "Пора"})
    return Response(speech=f"Таймер на {_human(seconds)}")


async def _left(ctx, slots) -> Response:
    """Сколько осталось до звонка.

    Человек спрашивает, чтобы понять, стоит ли ждать. Ответ «таймер не
    запущен» и ответ «осталось три минуты» — разные вещи, и молчать
    вместо ответа нельзя: человек решит, что его не расслышали.
    """
    try:
        left = await ctx.pending()
    except Exception:                              # noqa: BLE001
        left = {}

    if not isinstance(left, dict) or PLAN_ID not in left:
        return Response(speech="Таймер не запущен")

    seconds = max(0, int(left[PLAN_ID]))
    if seconds <= 0:
        return Response(speech="Таймер вот-вот сработает")
    return Response(speech=f"Осталось {_human(seconds)}")


async def _on_fire(ctx, slots) -> Response:
    """Обработчик звонка ядра. Текст приходит в слотах."""
    why = (slots or {}).get("why") or "Время вышло"
    return Response(speech=why)


async def _cancel(ctx, slots) -> Response:
    """Снять таймер. Отсутствие — не ошибка."""
    removed = await ctx.cancel(PLAN_ID)
    if not removed:
        return Response(speech="Таймер не запущен")
    return Response(speech="Таймер отменён")


async def on_fire(payload) -> Response:
    """Ядро разбудило плагин в срок.

    Возвращает, что сказать, — показывает ядро. Изнутри обработчика
    ходить в ядро нельзя: главный цикл плагина занят этим обработчиком
    и не читает ввод, а обращение к возможности ждёт ответа именно оттуда.
    Плагин зависал намертво, и человек не слышал звонка.

    Обработчик не должен падать — упав, он потеряет и остальные сроки.
    """
    why = (payload or {}).get("why") or "Время вышло"
    return Response(speech=why, title="Таймер")


_CONTEXT = None


def _current_context():
    return _CONTEXT


def register(registry) -> None:
    """Объявить навыки таймера и запомнить контекст для звонков."""
    from sinaps.skills.base import Skill, SlotSpec

    global _CONTEXT

    skill = Skill("timer", description="Таймер и напоминание через N минут")

    skill.command(
        "set", _start,
        patterns=[r"^(?:поставь|запусти|включи)\s+таймер\s*(?:на)?\s*(?P<amount>.+)$",
                  r"^(?:поставь|запусти|включи)\s+звонок\s*(?:на)?\s*(?P<amount>.+)$",
                  r"^звонок\s+(?:через\s+)?(?P<amount>.+)$",
                  r"^напомни\s+(?:через\s+)?(?P<amount>.+)$"],
        slots={"amount": SlotSpec(name="amount", type="string", required=True,
                                  description="Сколько ждать: «пять минут», «10 минут»")},
        examples=["поставь таймер на пять минут", "поставь звонок на 10 минут",
                  "напомни через 10 минут"],
        description="Поставить таймер. Ассистент скажет вслух, когда время.")

    skill.command(
        "left", _left,
        patterns=[r"^сколько\s+осталось\s+(?:таймера|звонка|времени)$",
                  r"^сколько\s+осталось$",
                  r"^через\s+сколько\s+(?:таймер|звонок)\s+сработает$",
                  r"^осталось\s+сколько\s+времени$"],
        examples=["сколько осталось таймера"],
        description="Сколько осталось до звонка.")

    # Команда, которой человек не может сказать: её зовёт ядро, когда
    # срок настал. Отдельный кадр протокола тут был бы вторым
    # читателем ввода, а читатель один.
    skill.command(
        "on_fire", _on_fire,
        patterns=[r"(?!)"],
        examples=[],
        description="Внутренняя команда: ядро зовёт плагин в срок.")

    skill.command(
        "cancel", _cancel,
        patterns=[r"^отмени\s+(?:таймер|звонок)$",
                  r"^отмена\s+(?:таймера|звонка)$"],
        examples=["отмени таймер", "отмени звонок"],
        description="Отменить запущенный таймер.")

    registry.add(skill)

    _bind_context(registry)


def _bind_context(registry) -> None:
    """Запомнить контекст, чтобы `on_fire` мог заговорить.

    Обработчик звонка приходит без контекста, а уведомление требует
    правы `notify` — значит контекст надо удержать при регистрации.
    """
    global _CONTEXT

    context = getattr(registry, "_context", None) or getattr(registry, "context", None)
    if context is not None:
        _CONTEXT = context