# -*- coding: utf-8 -*-
"""Таймер: «через пять минут напомни».

Честное ограничение, о котором надо сказать вслух: таймер живёт в
процессе плагина, а процесс плагина живёт, пока жив ассистент. После
перезапуска таймер не сработает. Напоминание, переживающее перезапуск,
— это отдельная работа с состоянием; здесь она не сделана, и обещать
её нельзя.

Разбудить плагин должен ядро, а не он сам: пока ядро умеет только
отвечать на услышанное, задача «позвонить через пять минут» не имеет
исполнителя. Фоновая задача в плагине — честный минимум, который
работает при работающем ассистенте.
"""

from __future__ import annotations

import asyncio
import re
import time

from sinaps.skills.base import Response

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

_TASK: asyncio.Task | None = None
_STATE: dict | None = None
_NOTIFY = None


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
    if "минут" in phrase or "минуту" in phrase or "минут" in phrase:
        return 60
    return None


def _seconds(text: str) -> int | None:
    """Сколько секунд просил человек. `None` — не понял."""
    lowered = (text or "").strip().lower()

    # «полчаса» содержит «час», и без этой проверки получается 30 часов
    # вместо получаса: таймер сработает через сутки.
    if "полчаса" in lowered or "пол часа" in lowered:
        return 1800

    # Голое «час» или «часов» — это час, а не «час, а сколько минут».
    if "час" in lowered and not any(w in lowered for w in _NUMBERS):
        return 3600

    # Голое «минуту», «минут» — это минута, а не «минут, а сколько».
    if ("минут" in lowered and not any(w in lowered for w in _NUMBERS)
            and not re.search(r"\d+", lowered)):
        return 60

    value = None
    match = re.search(r"(\d+)", lowered)
    if match:
        value = int(match.group(1))
    else:
        for word, seconds in sorted(_NUMBERS.items(), key=lambda kv: -len(kv[0])):
            if word in lowered:
                value = seconds
                break

    if value is None or value <= 0:
        return None

    factor = _unit_seconds(lowered)
    if factor is None:
        factor = 60                      # «через пять» — это минуты

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


async def _fire() -> None:
    global _TASK, _STATE
    try:
        while _STATE is not None and _STATE.get("deadline"):
            deadline = _STATE["deadline"]
            delay = deadline - _asyncio_now()
            if delay <= 0:
                break
            await asyncio.sleep(delay)

        if _STATE is None or not _STATE.get("deadline"):
            return
        if _NOTIFY is not None:
            await _NOTIFY(_STATE.get("label") or "Время вышло", title="Таймер")
    except asyncio.CancelledError:
        raise
    finally:
        _TASK = None
        if _STATE is not None:
            _STATE["deadline"] = 0


def _asyncio_now() -> float:
    """Монотонные часы, а не время цикла событий.

    Срок таймера не должен зависеть от того, запущен ли цикл: иначе
    постановка вне работающего цикла падала бы, а не просто молчала.
    """
    return time.monotonic()


def _set(ctx, seconds: int, label: str) -> "Response":
    global _TASK, _STATE, _NOTIFY

    _NOTIFY = getattr(ctx, "notify", None)
    if _STATE is None:
        _STATE = {"deadline": 0.0, "label": ""}
    _STATE["deadline"] = _asyncio_now() + seconds
    _STATE["label"] = label

    if _TASK is not None and not _TASK.done():
        _TASK.cancel()
        _TASK = None

    # Без работающего цикла ждать некому: таймер ставится, но сработать
    # не сможет. Лучше сказать об этом вслух, чем принять и забыть —
    # человек будет ждать звонка, которого не будет.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _STATE["deadline"] = 0.0
        return Response(speech="Сейчас не могу поставить таймер: "
                              "нет работающего цикла.")
    _TASK = loop.create_task(_fire())

    return Response(speech=f"Таймер на {_human(seconds)}")


def _cancel(ctx, slots) -> "Response":
    global _TASK, _STATE
    if _STATE is None or not _STATE.get("deadline"):
        return Response(speech="Таймер не запущен")
    _STATE["deadline"] = 0.0
    if _TASK is not None and not _TASK.done():
        _TASK.cancel()
        _TASK = None
    return Response(speech="Таймер отменён")


def register(registry) -> None:
    """Объявить навыки таймера."""
    from sinaps.skills import Skill
    from sinaps.skills.base import SlotSpec

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


async def _start(ctx, slots) -> "Response":
    """Поставить таймер."""
    seconds = _seconds((slots or {}).get("amount") or "")
    if seconds is None:
        return Response(speech=("Не понял, на сколько. Скажите: «поставь "
                                "таймер на пять минут»."))
    return _set(ctx, seconds, "Пора")