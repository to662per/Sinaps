# -*- coding: utf-8 -*-
"""Разбор времени: длительность, время суток, день, вместе.

Проверяется отдельно от плагинов. Разбор «завтра в девять» — это
 половина работы напоминания, и ошибка в нём выглядит как «напоминание
сработало не когда надо»: человек получил звонок в три часа ночи или
не получил вовсе.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sinaps.core.when import (human_duration, parse_clock, parse_day,
                              parse_duration, parse_when)


@pytest.fixture
def now():
    """Среда, 15 октября 2026, 14:20."""
    return datetime(2026, 10, 15, 14, 20)


# --- длительность ------------------------------------------------------------


@pytest.mark.parametrize(("фраза", "секунды"), [
    ("пять минут", 300),
    ("5 минут", 300),
    ("10 минут", 600),
    ("через час", 3600),
    ("час", 3600),
    ("полчаса", 1800),
    ("минуту", 60),
    ("тридцать секунд", 30),
    ("часа четыре", 4 * 3600),
])
def test_duration_is_understood(фраза, секунды):
    assert parse_duration(фраза) == секунды


@pytest.mark.parametrize("фраза", [
    "позже",
    "0 минут",
    "семьдесят два часа",
    "",
])
def test_unclear_duration_is_refused(фраза):
    """Непонятное время — это отказ, а не догадка.

    Догадка опаснее отказа: напоминание сработает не тогда, когда
    человек ждёт, и он решит, что забыл его поставить.
    """
    assert parse_duration(фраза) is None


# --- время суток -------------------------------------------------------------


@pytest.mark.parametrize(("фраза", "часы"), [
    ("в 15:30", (15, 30)),
    ("в 3 часа", (3, 0)),
    ("в 9 утра", (9, 0)),
    ("в 18:00", (18, 0)),
])
def test_clock_is_understood(фраза, часы):
    assert parse_clock(фраза) == часы


@pytest.mark.parametrize(("фраза", "часы"), [
    ("в девять", (9, 0)),
    ("в семь", (7, 0)),
    ("в полдень", (12, 0)),
])
def test_clock_in_words(фраза, часы):
    """Час словами — как его и говорят.

    «В девять» — обычная фраза человека, а разбирать её как «времени
    нет» было бы странно: человек сказал всё ясно.
    """
    assert parse_clock(фраза) == часы


@pytest.mark.parametrize(("фраза", "часы"), [
    ("в 7 вечера", (19, 0)),
    ("в 7 утра", (7, 0)),
    ("в 12 ночи", (0, 0)),
    ("в 12 дня", (12, 0)),
])
def test_part_of_day_is_understood(фраза, часы):
    """«Вечера» и «утра» меняют час.

    Без этого «в 7 вечера» означало бы семь утра, и напоминание
    сработало бы на восемь часов раньше.
    """
    assert parse_clock(фраза) == часы


def test_impossible_clock_is_refused():
    assert parse_clock("в 45:99") is None
    assert parse_clock("в 99 часов") is None


# --- день --------------------------------------------------------------------


def test_days_are_understood(now):
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    assert parse_day("сегодня", now) == today
    assert parse_day("завтра", now) == today + timedelta(days=1)
    assert parse_day("послезавтра", now) == today + timedelta(days=2)


def test_weekday_means_the_next_one(now):
    """«В понедельник» в понедельник — это через неделю.

    Сегодня среда, и «в понедельник» — это уже наступивший понедельник
    в прошлом. Человек говорит о будущем.
    """
    target = parse_day("в понедельник", now)

    assert target is not None
    assert target > now
    assert target.weekday() == 0
    assert (target - now).days <= 7


# --- вместе ------------------------------------------------------------------


def test_tomorrow_morning(now):
    target = parse_when("завтра в 9 утра", now)

    assert target == datetime(2026, 10, 16, 9, 0)


def test_today_evening(now):
    target = parse_when("сегодня в 18:30", now)

    assert target == datetime(2026, 10, 15, 18, 30)


def test_past_hour_means_tomorrow(now):
    """«В 3 часа» в пять вечера — это завтра в три.

    Иначе человек получил бы звонок в три часа ночи, чего не хотел
    ни он, ни его будильник.
    """
    target = parse_when("в 3 часа", now)

    assert target == datetime(2026, 10, 16, 3, 0)


def test_relative_is_understood(now):
    target = parse_when("через 5 минут", now)

    assert target == now + timedelta(minutes=5)


def test_day_without_time_is_morning(now):
    """«Завтра» без времени — это завтра утром.

    Сегодня вечер, и «завтра» в три часа ночи означало бы «завтра
    ночью» — так никто не говорит.
    """
    target = parse_when("завтра", now)

    assert target == datetime(2026, 10, 16, 9, 0)


def test_nonsense_is_refused(now):
    assert parse_when("когда-нибудь", now) is None
    assert parse_when("", now) is None


# --- как это звучит ----------------------------------------------------------


@pytest.mark.parametrize(("секунды", "ожидание"), [
    (0, "0 секунд"),
    (1, "1 секунда"),
    (2, "2 секунды"),
    (5, "5 секунд"),
    (11, "11 секунд"),
    (21, "21 секунда"),
    (60, "1 минута"),
    (120, "2 минуты"),
    (300, "5 минут"),
    (3600, "1 час"),
    (7200, "2 часа"),
    (86400, "24 часа"),
    (90, "1 минута 30 секунд"),
])
def test_duration_is_spoken_correctly(секунды, ожидание):
    """Формы числительных — как в речи.

    Ассистент говорит вслух, и «осталось 1 минут» — ошибка, которую
    слышно: человек решит, что таймер сломался.
    """
    assert human_duration(секунды) == ожидание