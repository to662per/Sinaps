# -*- coding: utf-8 -*-
"""Разбор того, когда человек просит напомнить.

В ядре, а не в плагинах, потому что плагинов с напоминаниями будет
несколько, и у каждого своя копия разбора — это четыре разных ответа на
одну и ту же фразу.

Что понимается:

* длительность — «пять минут», «10 минут», «час», «полчаса»;
* время суток — «в 15:30», «в 3 часа», «в половине четвёртого»;
* день — «сегодня», «завтра», «послезавтра», «в понедельник»;
* вместе — «завтра в 9 утра», «в среду в 18:00».

Чего нет намеренно: «через пару дней», «на следующей неделе», «когда
будет свободно». Такие фразы разбирать нельзя наугад: время,
придуманное вместо непонятого, хуже отказа — человек отложит важное
и забудет.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

#: Числа словами, которые говорит человек вслух.
_NUMBERS = {
    "один": 1, "одну": 1, "одна": 1, "раз": 1,
    "два": 2, "две": 2,
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "пятнадцать": 15,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "полчаса": 30, "полтора": 90,
}

#: Дни недели: человек говорит в именительном, мы считаем с понедельника.
_WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2,
    "четверг": 3, "пятница": 4, "пятницу": 4,
    "суббота": 5, "субботу": 5, "воскресенье": 6, "воскресенья": 6,
}

MAX_SECONDS = 6 * 60 * 60          # не больше шести часов «через»


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Правильная форма числительного: 1 минута, 3 минуты, 5 минут.

    Ассистент говорит вслух, и «осталось 3 минуты» звучит как ошибка.
    """
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def human_duration(seconds: int) -> str:
    """Сколько времени — словами и с правильными формами.

    Голосом это читается вслух, поэтому «1 минут 30 секунд» — ошибка,
    которую слышно. Часы, минуты и секунды собираются вместе: «час
    двадцать минут» короче и понятнее, чем «4800 секунд».
    """
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)

    parts: list[str] = []
    if hours:
        parts.append(f"{hours} {_plural(hours, 'час', 'часа', 'часов')}")
    if minutes:
        parts.append(f"{minutes} {_plural(minutes, 'минута', 'минуты', 'минут')}")
    if secs or not parts:
        parts.append(f"{secs} {_plural(secs, 'секунда', 'секунды', 'секунд')}")
    return " ".join(parts)


def _unit_seconds(phrase: str) -> int | None:
    """Во сколько секунд обходится одна единица из фразы.

    Единица ищется по всей фразе, а не по хвосту после числа: человек
    говорит «часа четыре», где число стоит в конце. Искать только с
    конца — значит «часа четыре» превратится в четыре минуты, и
    напоминание сработает в шестьдесят раз раньше срока.
    """
    if "секунд" in phrase or "секун" in phrase:
        return 1
    if "час" in phrase:
        return 3600
    if "минут" in phrase:
        return 60
    return None


def parse_duration(text: str) -> int | None:
    """Сколько секунд просил человек. `None` — не понял.

    Длительность без слова «через» тоже читается: «пять минут» —
    это про то же самое.
    """
    lowered = (text or "").strip().lower()

    # «полчаса» содержит «час», и без этой проверки получается 30 часов
    # вместо получаса: напоминание сработает через сутки.
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
    if seconds <= 0 or seconds > MAX_SECONDS:
        return None
    return seconds


def parse_clock(text: str) -> tuple[int, int] | None:
    """Время суток из фразы. `None` — времени там не было.

    Понимаются «15:30», «в 3 часа», «в 9 утра», «в половине четвёртого».
    """
    lowered = (text or "").strip().lower()

    match = re.search(r"(\d{1,2})[:.](\d{2})", lowered)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None

    # Час словами: «в 3 часа», «в 9 утра», «в 7 вечера», «в 5 ночи».
    # Слово-подсказка (утра/вечера) нужна, чтобы отличить «в 7» от
    # «в 19»: человек говорит «в 7 вечера», а «в 7 утра» — другое время.
    match = re.search(r"(\d{1,2})\s*(час\w*|ч\b|утр\w*|дн\w*|вечер\w*|ноч\w*)",
                      lowered)
    if match:
        hour = int(match.group(1))
        if not 0 <= hour <= 23:
            return None
        word = match.group(2)
        if word.startswith(("вечер", "ноч")) and hour < 12:
            hour += 12
        elif word.startswith("ноч") and hour == 12:
            hour = 0          # полночь — это ноль часов
        elif word.startswith("утр") and hour == 12:
            hour = 0
        return hour, 0

    # Час словами: «в девять», «в семь вечера». Люди говорят именно
    # так, и «девять» без разбора выглядело бы как отсутствие времени.
    for word, number in sorted(_NUMBERS.items(), key=lambda kv: -len(kv[0])):
        if word not in lowered:
            continue
        position = lowered.find(word)
        tail = lowered[position + len(word):]
        hint = re.match(r"\s*(час\w*|утр\w*|вечер\w*|ноч\w*|дн\w*)", tail)
        if not hint:
            # «В девять» — без слова «час». Слово-подсказка не
            # обязательна, но число должно идти сразу после «в»:
            # иначе «в два часа» и «в два» — одно и то же, а
            # «позвонить в два» вовсе не про час.
            before = lowered[:position].rstrip()
            if not before.endswith("в"):
                continue
        if number > 23:
            return None
        kind = hint.group(1) if hint else ""
        if kind.startswith(("вечер", "ноч")) and number < 12:
            number += 12
        elif kind.startswith("утр") and number == 12:
            number = 0
        return number, 0

    words = {"половине": (12, 0), "полвтором": (12, 30),
             "половина": (12, 0), "полвторого": (12, 30),
             "полдень": (12, 0), "полночь": (0, 0), "полночи": (0, 0)}
    for word, value in words.items():
        if word in lowered:
            return value

    return None


def parse_day(text: str, now: datetime | None = None) -> datetime | None:
    """День из фразы: «сегодня», «завтра», «послезавтра», «в понедельник».

    Возвращает начало этого дня. `None` — дня там не было.
    """
    lowered = (text or "").strip().lower()
    today = (now or datetime.now()).replace(hour=0, minute=0, second=0,
                                            microsecond=0)

    if "послезавтра" in lowered:
        return today + timedelta(days=2)
    if "завтра" in lowered:
        return today + timedelta(days=1)
    if "сегодня" in lowered:
        return today

    for word, index in _WEEKDAYS.items():
        if word in lowered:
            ahead = (index - today.weekday()) % 7
            if ahead == 0:
                ahead = 7        # «в понедельник» в понедельник — это
            return today + timedelta(days=ahead)   # через неделю, а не сегодня
    return None


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """Когда прозвонить. `None` — непонятно.

    Сначала ищется конкретный момент («завтра в 9»), потом день без
    времени («завтра» — это ближайшее утро), и только потом длительность
    («через пять минут»).
    """
    moment = now or datetime.now()
    lowered = (text or "").strip().lower()

    if not lowered:
        return None

    clock = parse_clock(lowered)
    day = parse_day(lowered, moment)

    if clock is not None:
        hour, minute = clock
        if day is None:
            day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            target = day + timedelta(hours=hour, minutes=minute)
            # Час уже прошёл — значит, на завтра. Иначе «в 3 часа» в
            # пять вечера означало бы «в три часа ночи», что никто не
            # имел в виду.
            if target <= moment:
                target += timedelta(days=1)
            return target
        return day + timedelta(hours=hour, minutes=minute)

    if day is not None:
        # День без времени — утро этого дня: «завтра» в восемь вечера
        # значит завтра утром, а не сегодня ночью.
        return day + timedelta(hours=9)

    seconds = parse_duration(lowered)
    if seconds is not None:
        return moment + timedelta(seconds=seconds)

    return None
