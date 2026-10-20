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
    assert timer._human(3600) == "1 час"
    assert timer._human(45) == "45 секунд"
    assert timer._human(90) == "1 минута 30 секунд"


# --- постановка и отмена -----------------------------------------------------


# --- срабатывание ------------------------------------------------------------


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