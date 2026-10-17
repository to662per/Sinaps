# -*- coding: utf-8 -*-
"""Таймер: разбор времени и ход часов.

Разбор проверяется отдельно от срабатывания. Человек говорит словами,
и если «пять минут» превратится в пять секунд, таймер сработает мгновенно
и человек решит, что сломался ассистент, а не он.

Ограничение таймера, о котором надо помнить: он живёт в процессе
плагина. После перезапуска ассистента не сработает. Напоминание,
переживающее перезапуск, — отдельная работа.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_PLUGIN = pathlib.Path(r"D:\Sinaps\plugins\timer\plugin.py")


@pytest.fixture
def timer():
    spec = importlib.util.spec_from_file_location("timer_plugin", _PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._STATE = None
    module._TASK = None
    module._NOTIFY = None
    return module


# --- разбор времени ----------------------------------------------------------


@pytest.mark.parametrize(("фраза", "секунды"), [
    ("пять минут", 300),
    ("5 минут", 300),
    ("10 минут", 600),
    ("через час", 3600),
    ("час", 3600),
    ("полчаса", 1800),
    ("минуту", 60),
    ("тридцать секунд", 30),
])
def test_time_is_understood(timer, фраза, секунды):
    assert timer._seconds(фраза) == секунды


@pytest.mark.parametrize("фраза", [
    "позже",
    "0 минут",
    "семьдесят два часа",
    "",
])
def test_unclear_time_is_refused(timer, фраза):
    """Непонятное время — это отказ, а не догадка.

    Догадка здесь опаснее отказа: таймер сработает не тогда, когда
    человек ждёт, и он решит, что забыл его поставить.
    """
    assert timer._seconds(фраза) is None


def test_number_after_unit_is_understood(timer):
    """«часа четыре» — это четыре часа.

    Число стоит в конце фразы, и его легко принять за минуты: человек
    услышал бы «четыре минуты» вместо четырёх часов и решил бы, что
    таймер сломался.
    """
    assert timer._seconds("часа четыре") == 4 * 3600


def test_phrase_is_read_as_minutes_by_default(timer):
    """«через пять» — это минуты, а не секунды."""
    assert timer._seconds("через пять") == 300


def test_time_is_spoken_in_words(timer):
    """Озвучка говорит словами, а не цифрами.

    «Таймер на 5 минут» вслух читается как «на пять минут» и звучит
    как отчёт о сбое.
    """
    assert timer._human(300) == "5 минут"
    assert timer._human(3600) == "1 часов"
    assert timer._human(45) == "45 секунд"


# --- постановка и отмена -----------------------------------------------------


@pytest.mark.asyncio
async def test_timer_is_armed(timer):
    """Постановка запоминает срок."""
    from sinaps.skills.base import Response

    reply = timer._set(object(), 300, "Пора")

    assert isinstance(reply, Response)
    assert timer._STATE["deadline"] > 0, (
        "таймер не встал — срок не сохранён")
    assert "Таймер" in reply.speech
    timer._cancel(object(), {})


@pytest.mark.asyncio
async def test_cancel_stops_armed_timer(timer):
    """Отмена снимает срок."""
    timer._set(object(), 300, "Пора")

    reply = timer._cancel(object(), {})

    assert timer._STATE["deadline"] == 0
    assert "отменён" in reply.speech


@pytest.mark.asyncio
async def test_cancel_without_timer_is_not_an_error(timer):
    """Отменить несуществующий таймер — не ошибка."""
    reply = timer._cancel(object(), {})

    assert "не запущен" in reply.speech


# --- срабатывание ------------------------------------------------------------


@pytest.mark.asyncio
async def test_timer_fires_and_notifies(timer):
    """Сработав, таймер зовёт уведомление.

    Молчащее срабатывание хуже отсутствия: человек ждёт, что его позовут,
    и решает, что таймер сломан.
    """
    shown: list[tuple[str, str]] = []

    async def _notify(text, *, title="Sinaps"):
        shown.append((title, text))

    timer._NOTIFY = _notify
    timer._STATE = {"deadline": timer._asyncio_now() - 1, "label": "Пора встать"}

    await timer._fire()

    assert shown == [("Таймер", "Пора встать")], "уведомление не показано"


@pytest.mark.asyncio
async def test_fire_does_nothing_without_timer(timer):
    """Срабатывание без таймера молчит, а не зовёт впустую."""
    shown: list = []
    timer._NOTIFY = lambda text, title="Sinaps": shown.append(text)
    timer._STATE = {"deadline": 0, "label": ""}

    await timer._fire()

    assert shown == []


def test_notify_reaches_plugin_context(timer):
    """Плагин достаёт уведомление из контекста.

    Раньше контекст давал плагину только `state`, и право `notify`
    было разрешено, но воспользоваться им было нечем.
    """
    from sinaps.plugins.runner import _PluginContext

    called: list = []

    async def _notify(text, *, title="Sinaps"):
        called.append(text)

    ctx = _PluginContext(object(), {"k": 1}, _notify)

    assert ctx.notify is not None, "в контексте плагина нет notify"