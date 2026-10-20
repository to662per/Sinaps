# -*- coding: utf-8 -*-
"""«Сколько осталось» и «звонок» — тем же способом, что и таймер.

Человек спрашивает, сколько ждать, чтобы понять, стоит ли. Ответ «таймер
не запущен» и ответ «осталось три минуты» — разные вещи, и молчать
вместо ответа нельзя: человек решит, что его не расслышали.

Формы числительных проверяются отдельно: ассистент говорит вслух, и
«осталось 1 минут» — ошибка, которую слышно.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_PLUGIN = pathlib.Path(r"D:\Sinaps\plugins\timer\plugin.py")


@pytest.fixture
def timer():
    spec = importlib.util.spec_from_file_location("timer_plugin3", _PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Ctx:
    """Контекст, который отвечает заранее заданным."""

    def __init__(self, pending=None, left=None):
        self._pending = pending or {}
        self.calls: list[tuple] = []
        self._left = left

    async def pending(self):
        self.calls.append(("pending",))
        return self._pending

    async def schedule(self, plan_id, delay_s, payload=None):
        self.calls.append(("schedule", plan_id, delay_s))
        return 0.0

    async def cancel(self, plan_id):
        self.calls.append(("cancel", plan_id))
        return True


# --- формы слов --------------------------------------------------------------


@pytest.mark.parametrize(("seconds", "ожидание"), [
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
def test_time_is_spoken_correctly(timer, seconds, ожидание):
    """Формы числительных — как в речи, а не как в таблице.

    «1 минут» вслух слышится как ошибка, и человек решает, что таймер
    сломался, а не что слово скривилось.
    """
    assert timer._human(seconds) == ожидание


# --- «сколько осталось» -----------------------------------------------------


@pytest.mark.asyncio
async def test_left_reports_remaining(timer):
    """Остаток отвечает словами и с правильной формой."""
    ctx = _Ctx(pending={"timer-1": 185})

    reply = await timer._left(ctx, {})

    assert "Осталось" in reply.speech
    assert "3 минуты" in reply.speech, reply.speech
    assert "5 секунд" in reply.speech, reply.speech


@pytest.mark.asyncio
async def test_left_without_timer_says_so(timer):
    """Нет таймера — так и говорим, а не молчим."""
    ctx = _Ctx(pending={})

    reply = await timer._left(ctx, {})

    assert "не запущен" in reply.speech


@pytest.mark.asyncio
async def test_left_does_not_fail_on_broken_context(timer):
    """Если сроки не спросились — отвечаем, а не падаем.

    Падение здесь означало бы, что ассистент молчит на прямой вопрос.
    """
    class _Broken(_Ctx):
        async def pending(self):
            raise RuntimeError("ядро недоступно")

    reply = await timer._left(_Broken(), {})

    assert reply.speech


# --- «звонок» ставится так же, как таймер ------------------------------------


@pytest.mark.asyncio
async def test_bell_is_set_like_a_timer(timer):
    """«Поставь звонок» — тот же механизм, что и «поставь таймер».

    Отдельный будильник в отдельном плагине означал бы второе
    хранилище сроков и вторую обработку перезапуска. Звонок и таймер —
    одно и то же дело, и человек не должен знать, как оно устроено.
    """
    ctx = _Ctx()

    reply = await timer._start(ctx, {"amount": "пять минут"})

    assert ("schedule", timer.PLAN_ID, 300) in ctx.calls
    assert "Таймер" in reply.speech


# --- команды объявлены -------------------------------------------------------


def test_commands_are_declared(timer):
    """Все четыре команды на месте: поставить, спросить, отменить, звонок."""
    import sys

    sys.path.insert(0, r"D:\Sinaps")
    from sinaps.skills.base import Skill, SlotSpec
    from sinaps.skills.registry import SkillRegistry

    registry = SkillRegistry()
    timer.register(registry)

    names = {spec.name for spec in registry.specs}

    # Имена с префиксом навыка: ядро зовёт плагин по полному имени,
    # и «timer.on_fire» — тот самый, которым зовёт ядро.
    assert "timer.set" in names
    assert "timer.left" in names
    assert "timer.cancel" in names
    assert "timer.on_fire" in names

# --- разбор живой речи -------------------------------------------------------


@pytest.fixture
def registry(timer):
    """Реестр с объявленными навыками таймера."""
    import sys

    sys.path.insert(0, r"D:\Sinaps")
    from sinaps.skills.registry import SkillRegistry

    reg = SkillRegistry()
    timer.register(reg)
    return reg


@pytest.mark.parametrize("фраза", [
    "поставь таймер на пять минут",
    "поставь звонок на пять минут",
    "звонок через 10 минут",
    "запусти таймер на 2 минуты",
    "напомни через 10 минут",
])
def test_bell_and_timer_are_the_same_thing(registry, фраза):
    """«Звонок» и «таймер» — одна и та же команда.

    Человек не должен знать, как устроено изнутри. Отдельный будильник
    означал бы второе хранилище сроков и вторую обработку перезапуска.
    """
    match = registry.match(фраза)

    assert match is not None, f"фраза не разобрана: {фраза!r}"
    assert match.spec.name == "timer.set", match.spec.name


@pytest.mark.parametrize("фраза", [
    "сколько осталось таймера",
    "сколько осталось",
    "сколько осталось звонка",
    "через сколько таймер сработает",
])
def test_remaining_question_is_recognised(registry, фраза):
    """Вопрос про остаток узнаётся."""
    match = registry.match(фраза)

    assert match is not None, f"фраза не разобрана: {фраза!r}"
    assert match.spec.name == "timer.left", match.spec.name


def test_cancel_understands_bell(registry):
    """«Отмени звонок» — тоже отмена."""
    match = registry.match("отмени звонок")

    assert match is not None
    assert match.spec.name == "timer.cancel", match.spec.name
