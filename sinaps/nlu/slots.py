"""Слоты: превращают слова в числа, длительности и время.

Это самая недооценённая часть понимания речи. «Поставь таймер на пять минут»
— это не число, это «пять минут», и без разбора слов навык не сможет
поставить таймер.

Здесь нет машинного обучения: конечный словарь + регулярные выражения.
Этого достаточно для большинства реальных фраз и, в отличие от LLM,
работает мгновенно и предсказуемо.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta

from ..core.errors import InvalidSlot

#: Множители длительности.
_UNITS = {
    "секунда": 1, "секунды": 1, "секунду": 1, "секунд": 1, "сек": 1, "с": 1,
    "минута": 60, "минуты": 60, "минуту": 60, "минут": 60, "мин": 60,
    "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
    "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400,
    "неделя": 604800, "недели": 604800, "недель": 604800,
}

_NUMBERS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400,
    "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800,
    "девятьсот": 900, "тысяча": 1000,
}

_COMPOUND_TENS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
}

_DURATION_RE = re.compile(
    r"(?P<amount>\d+|" + "|".join(sorted(_NUMBERS, key=len, reverse=True)) + r")"
    r"\s*(?P<unit>" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")"
    r"(?:\s*(?P<amount2>\d+)\s*(?P<unit2>" +
    "|".join(sorted(_UNITS, key=len, reverse=True)) + r"))?",
    re.IGNORECASE,
)

_TIME_RE = re.compile(
    r"\b(?P<h>\d{1,2})[:.](?P<m>\d{2})\b|"
    r"\bв\s+(?P<hh>\d{1,2})\s*(?:час(?:ов|а)?|ч)\b(?:\s*(?P<mm>\d{1,2})\s*(?:мин(?:ут)?)?)?",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(
    r"-?\d+(?:[.,]\d+)?|" + "|".join(sorted(_NUMBERS, key=len, reverse=True)),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Duration:
    """Длительность с человекочитаемым текстом."""

    seconds: int
    original: str = ""

    def humanize(self) -> str:
        """«5 минут», «1 час 30 минут» — для озвучки."""
        seconds = self.seconds
        if seconds < 60:
            return _plural(seconds, "секунда", "секунды", "секунд")

        parts: list[str] = []
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)

        if hours:
            parts.append(_plural(hours, "час", "часа", "часов"))
        if minutes:
            parts.append(_plural(minutes, "минута", "минуты", "минут"))
        if secs and not hours:
            parts.append(_plural(secs, "секунда", "секунды", "секунд"))
        return " ".join(parts)

    def until(self, now: datetime | None = None) -> datetime:
        return (now or datetime.now()) + timedelta(seconds=self.seconds)


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русские склонения: 1 час, 2 часа, 5 часов."""
    n100, n10 = count % 100, count % 10
    if 11 <= n100 <= 19:
        return f"{count} {many}"
    if n10 == 1:
        return f"{count} {one}"
    if 2 <= n10 <= 4:
        return f"{count} {few}"
    return f"{count} {many}"


# --- числа прописью ---------------------------------------------------------
#
# Живут здесь, а не в tts, потому что это разбор текста: им пользуются и
# синтезатор, и навыки («работает 2 дня 3 часа»).

_ONES = {
    0: "ноль", 1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять",
    6: "шесть", 7: "семь", 8: "восемь", 9: "девять",
}

#: 11–19 — самостоятельные слова. «десять пять» по-русски не говорят.
_TEENS = {
    11: "одиннадцать", 12: "двенадцать", 13: "тринадцать", 14: "четырнадцать",
    15: "пятнадцать", 16: "шестнадцать", 17: "семнадцать", 18: "восемнадцать",
    19: "девятнадцать",
}

_TENS = {
    2: "двадцать", 3: "тридцать", 4: "сорок", 5: "пятьдесят",
    6: "шестьдесят", 7: "семьдесят", 8: "восемьдесят", 9: "девяносто",
}

_HUNDREDS = {
    1: "сто", 2: "двести", 3: "триста", 4: "четыреста", 5: "пятьсот",
    6: "шестьсот", 7: "семьсот", 8: "восемьсот", 9: "девятьсот",
}

#: Тысячи — женский род: «одна тысяча», «две тысячи».
_FEMININE_ONES = {1: "одна", 2: "две"}


def number_to_words(num: int) -> str:
    """Число прописью по-русски."""
    num = int(num)
    if num < 0:
        return "минус " + number_to_words(-num)
    if num == 0:
        return "ноль"

    words: list[str] = []
    for value, forms in (
        (1_000_000_000, ("миллиард", "миллиарда", "миллиардов")),
        (1_000_000, ("миллион", "миллиона", "миллионов")),
        (1_000, ("тысяча", "тысячи", "тысяч")),
    ):
        count, num = divmod(num, value)
        if not count:
            continue
        # Для тысяч нужна женская форма: «одна тысяча», «две тысячи».
        words.extend(_words_under_1000(count, feminine=(value == 1_000)))
        words.append(_plural_form(count, forms))
    if num:
        words.extend(_words_under_1000(num))
    return " ".join(w for w in words if w)


def _words_under_1000(num: int, *, feminine: bool = False) -> list[str]:
    """345 -> ['триста', 'сорок', 'пять']."""
    words: list[str] = []
    hundreds, rest = divmod(num, 100)
    if hundreds:
        words.append(_HUNDREDS[hundreds])

    tens, ones = divmod(rest, 10)
    if 11 <= rest <= 19:
        return words + [_TEENS[rest]]
    if tens == 1:
        words.append("десять")
    elif tens > 1:
        words.append(_TENS[tens])
    if ones:
        if feminine and ones in _FEMININE_ONES:
            words.append(_FEMININE_ONES[ones])
        else:
            words.append(_ONES[ones])
    return words


def _plural_form(count: int, forms: tuple[str, str, str]) -> str:
    """Выбор формы существительного при числительном."""
    n100 = count % 100
    n10 = count % 10
    if 11 <= n100 <= 19:
        return forms[2]
    if n10 == 1:
        return forms[0]
    if 2 <= n10 <= 4:
        return forms[1]
    return forms[2]


def parse_number(text: str, *, default: float | None = None) -> float:
    """Число из строки: «15», «пятнадцать», «двадцать пять»."""
    if text is None:
        raise InvalidSlot("number", text)

    text = text.strip().lower()
    if not text:
        if default is not None:
            return default
        raise InvalidSlot("number", text, "пустое значение")

    # Обычное число (в том числе «-3,5»).
    try:
        return float(text.replace(",", "."))
    except ValueError:
        pass

    tokens = re.findall(r"[а-яёa-z]+|\d+(?:[.,]\d+)?", text)
    total = 0
    found = False

    for token in tokens:
        if token in _NUMBERS:
            total += _NUMBERS[token]
            found = True
        elif token in _COMPOUND_TENS:
            total += _COMPOUND_TENS[token]
            found = True
        else:
            try:
                total += float(token.replace(",", "."))
                found = True
            except ValueError:
                continue

    if not found:
        if default is not None:
            return default
        raise InvalidSlot("number", text, "не найдено число")
    return float(total)


def parse_duration(text: str) -> Duration:
    """Длительность из текста: «5 минут», «час полтора», «90 секунд»."""
    if not text:
        raise InvalidSlot("duration", text, "пустое значение")

    cleaned = text.strip().lower()
    total = 0
    found = False

    for match in _DURATION_RE.finditer(cleaned):
        amount = parse_number(match.group("amount"))
        unit = match.group("unit").lower()
        total += int(amount * _UNITS.get(unit, 1))
        found = True

        # Составная длительность: «1 час 30 минут».
        if match.group("amount2") and match.group("unit2"):
            total += int(parse_number(match.group("amount2")) *
                         _UNITS.get(match.group("unit2").lower(), 1))

    if not found:
        # «полчаса», «час» без числа.
        if "полчаса" in cleaned or "пол часа" in cleaned:
            return Duration(1800, cleaned)
        if re.search(r"\bчас[а-я]*\b", cleaned) and not re.search(r"\d", cleaned):
            return Duration(3600, cleaned)

        # Голое число трактуем как секунды — как в большинстве мессенджеров.
        try:
            return Duration(int(parse_number(cleaned)), cleaned)
        except InvalidSlot:
            pass

        raise InvalidSlot("duration", text, "не понял, сколько времени")

    return Duration(max(0, total), cleaned)


def parse_time(text: str, *, now: datetime | None = None) -> dt_time:
    """Время из текста: «7:30», «в 19 часов», «в 5 вечера»."""
    now = now or datetime.now()
    cleaned = (text or "").strip().lower()
    if not cleaned:
        raise InvalidSlot("time", text, "пустое значение")

    match = _TIME_RE.search(cleaned)
    if match:
        if match.group("h") is not None:
            hour, minute = int(match.group("h")), int(match.group("m"))
        else:
            hour = int(match.group("hh"))
            minute = int(match.group("mm") or 0)
        return _normalize_time(hour, minute, now)

    # «в 5 вечера», «в 17 часов»
    words = re.search(r"\b(\d{1,2})\s*(вечера|ночи|утра|дня)\b", cleaned)
    if words:
        hour = int(words.group(1))
        marker = words.group(2)
        if marker == "вечера" and hour < 12:
            hour += 12
        elif marker == "ночи" and hour < 12:
            hour += 12
        return _normalize_time(hour, 0, now)

    # Только словами: «семь тридцать»
    spoken = re.search(
        r"\b(" + "|".join(sorted(_NUMBERS, key=len, reverse=True)) + r")\s+"
        r"(" + "|".join(sorted(_NUMBERS, key=len, reverse=True)) + r")\b",
        cleaned,
    )
    if spoken:
        hour = int(parse_number(spoken.group(1)))
        minute = int(parse_number(spoken.group(2)))
        return _normalize_time(hour, minute, now)

    raise InvalidSlot("time", text, "не понял время")


def _normalize_time(hour: int, minute: int, now: datetime) -> dt_time:
    """Приводит 25:00 к следующим суткам и отсекает неверные значения."""
    if hour > 23 or minute > 59:
        # «25:00» -> час назад от полуночи следующего дня.
        total = hour * 60 + minute
        total %= 24 * 60
        hour, minute = divmod(total, 60)
    return dt_time(hour=hour, minute=minute)


def parse_datetime_slot(text: str, *, now: datetime | None = None) -> datetime:
    """Полные даты и время: «завтра в 7:30», «через 10 минут»."""
    now = now or datetime.now()
    cleaned = (text or "").strip().lower()

    result = now
    matched_relative = False

    if "послезавтра" in cleaned:
        result += timedelta(days=2)
        matched_relative = True
    elif "завтра" in cleaned:
        result += timedelta(days=1)
        matched_relative = True
    elif "сегодня" in cleaned:
        matched_relative = True

    time_match = _TIME_RE.search(cleaned)
    if time_match:
        if time_match.group("h") is not None:
            hour, minute = int(time_match.group("h")), int(time_match.group("m"))
        else:
            hour = int(time_match.group("hh"))
            minute = int(time_match.group("mm") or 0)
        return result.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)

    if matched_relative:
        return result.replace(hour=9, minute=0, second=0, microsecond=0)

    raise InvalidSlot("datetime", text, "не понял дату")


def extract_numbers(text: str) -> list[float]:
    """Все числа в тексте — для навыков вроде «сделай громкость на 50»."""
    return [parse_number(token) for token in _NUMBER_RE.findall(text or "")]
