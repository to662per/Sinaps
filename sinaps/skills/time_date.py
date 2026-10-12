"""Навык «Время и дата» — самый простой и самый полезный.

Начинаем с него намеренно: он не зависит ни от чего, кроме стандартной
библиотеки, и сразу показывает, что механизм навыков работает.
"""

from __future__ import annotations

from datetime import datetime

from ..skills.base import Response, Skill


def register() -> Skill:
    skill = Skill("time", description="Время и дата")

    async def what_time(ctx, slots):  # noqa: ANN001
        now = datetime.now()
        return Response(
            speech=_spoken_time(now),
            display=now.strftime("%H:%M:%S, %d.%m.%Y"),
        )

    async def what_date(ctx, slots):  # noqa: ANN001
        now = datetime.now()
        return Response(
            speech=_spoken_date(now),
            display=now.strftime("%d.%m.%Y, %A"),
        )

    async def what_day(ctx, slots):  # noqa: ANN001
        now = datetime.now()
        return Response(
            speech=_spoken_weekday(now),
            display=now.strftime("%A"),
        )

    skill.command(
        "now",
        what_time,
        patterns=[
            # Пробел между «какой» и «час» обязателен — именно на этом
            # спотыкалась первая версия шаблона.
            r"^(?:какой|который)\s+(?:час|время)\s*\??$",
            r"^(?:скажи|скажите)\s+(?:который\s+)?час$",
            r"^час\s*(?:сейчас)?\s*\??$",
            r"^сколько времени$",
            r"^время сейчас$",
            r"^какое время$",
            r"^what time is it$",
        ],
        examples=["который час", "сколько времени", "время сейчас"],
        description="Узнать текущее время",
        priority=10,
    )

    skill.command(
        "date",
        what_date,
        patterns=[
            r"^какая сегодня дата$",
            r"^дата$",
            r"^какое сегодня число$",
            r"^what(?:'s| is) the date$",
        ],
        examples=["какая сегодня дата", "какое сегодня число"],
        description="Узнать текущую дату",
        priority=10,
    )

    skill.command(
        "weekday",
        what_day,
        patterns=[
            r"^какой сегодня день(?: недели)?$",
            r"^какой день$",
            r"^what day is it$",
        ],
        examples=["какой сегодня день", "какой день недели"],
        description="Узнать день недели",
        priority=10,
    )

    return skill


_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)


def _spoken_time(now: datetime) -> str:
    """«Сейчас 14:05» — цифрами озвучивает синтезатор, так понятнее."""
    return f"Сейчас {now.strftime('%H:%M')}"


def _spoken_date(now: datetime) -> str:
    return f"Сегодня {now.day} {_month_name(now.month)} {_year_word(now.year)}"


def _spoken_weekday(now: datetime) -> str:
    return f"Сегодня {_WEEKDAYS[now.weekday()]}"


_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _month_name(month: int) -> str:
    return _MONTHS[month - 1]


def _year_word(year: int) -> str:
    """Год читают «две тысячи двадцать шестой», а не «2026»."""
    from ..nlu.slots import number_to_words

    high, low = divmod(year, 100)
    high_words, low_words = number_to_words(high), number_to_words(low)
    if low == 0:
        return f"{high_words} года"
    return f"{high_words} {low_words} года"
