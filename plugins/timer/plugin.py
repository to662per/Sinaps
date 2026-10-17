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

#: «пять», «5», «полчаса» — ровно то, что человек говорит вслух.
_NUMBERS = {
    "один": 1, "одну": 1, "одна": 1, "раз": 1,
    "два": 2, "две": 2,
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "пятнадцать": 15,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "полчаса": 30, "полтора": 90,
}

_MAX_SECONDS = 6 * 60 * 60          # не больше шести часов


def _unit_seconds(phrase: str) -> int | None:
    """Во сколько секунд обходится одна единица из фразы.

    Единица ищется по всей фразе, а не по хвосту после числа: человек
    говорит «часа четыре», где число стоит в конце. Искать только с
    конца — значит «часа четыре» превратится в четыре минуты, и
    таймер сработает в шестьдесят раз раньше срока.
    """
    if "секунд" in phrase or "секун" in phrase:
        return 1
    if "час" in phrase:
        return 3600
    if "минут" in phrase:
        return 60
    return None


def _seconds(text: str) -> int | None:
    """Сколько секунд просил человек. `None` — не понял."""
    lowered = (text or "").strip().lower()

    # «полчаса» содержит «час», и без этой проверки получается 30 часов
    # вместо получаса: таймер сработает через сутки.
    if "полчаса" in lowered or "пол часа" in lowered:
        return 1800

    if "час" in lowered and not any(w in lowered for w in _NUMBERS):
        return 3600

    if ("минут" in lowered and not any(w in lowered for w in _NUMBERS)
            and not re.search(r"\d+", lowered)):
        return 60

    value = None
    match = re.search(r"(\d+)", lowered)
    if match:
        value = int(match.group(1))
    else:
        for word, seconds in sorted(_NUMBERS.items(),
                                    key=lambda kv: -len(kv[0])):
            if word in lowered:
                value = seconds
                break

    if value is None or value <= 0:
        return None

    factor = _unit_seconds(lowered) or 60      # «через пять» — это минуты
    seconds = value * factor
    if seconds <= 0 or seconds > _MAX_SECONDS:
        return None
    return seconds


def _human(seconds: int) -> str:
    """Сколько осталось — словами, а не цифрами."""
    if seconds % 3600 == 0:
        return f"{seconds // 3600} часов"
    if seconds % 60 == 0:
        return f"{seconds // 60} минут"
    return f"{seconds} секунд"


async def _start(ctx, slots) -> Response:
    """Поставить таймер через расписание ядра."""
    seconds = _seconds((slots or {}).get("amount") or "")
    if seconds is None:
        return Response(speech=("Не понял, на сколько. Скажите: «поставь "
                                "таймер на пять минут»."))
    await ctx.schedule(PLAN_ID, seconds, {"why": "Пора"})
    return Response(speech=f"Таймер на {_human(seconds)}")


async def _cancel(ctx, slots) -> Response:
    """Снять таймер. Отсутствие — не ошибка."""
    removed = await ctx.cancel(PLAN_ID)
    if not removed:
        return Response(speech="Таймер не запущен")
    return Response(speech="Таймер отменён")


async def on_fire(payload) -> None:
    """Ядро разбудило плагин в срок.

    Здесь плагин показывает окно и говорит вслух: права `notify`.
    Обработчик не должен падать — упав, он потеряет и остальные сроки.
    """
    why = (payload or {}).get("why") or "Время вышло"
    ctx = _current_context()
    if ctx is not None and getattr(ctx, "notify", None) is not None:
        await ctx.notify(why, title="Таймер")


_CONTEXT = None


def _current_context():
    return _CONTEXT


def register(registry) -> None:
    """Объявить навыки таймера и запомнить контекст для звонков."""
    from sinaps.skills import Skill
    from sinaps.skills.base import SlotSpec

    global _CONTEXT

    skill = Skill("timer", description="Таймер и напоминание через N минут")

    skill.command(
        "set", _start,
        patterns=[r"^(?:поставь|запусти|включи)\s+таймер\s*(?:на)?\s*(?P<amount>.+)$",
                  r"^напомни\s+(?:через\s+)?(?P<amount>.+)$"],
        slots={"amount": SlotSpec(name="amount", type="string", required=True,
                                  description="Сколько ждать: «пять минут», «10 минут»")},
        examples=["поставь таймер на пять минут", "напомни через 10 минут"],
        description="Поставить таймер. Ассистент скажет вслух, когда время.")

    skill.command(
        "cancel", _cancel,
        patterns=[r"^отмени\s+таймер$", r"^отмена\s+таймера$"],
        examples=["отмени таймер"],
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