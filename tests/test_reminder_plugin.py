# -*- coding: utf-8 -*-
"""Напоминание: что напомнить, когда, список и отмена.

Три вещи, которые плагин делает, проверяются по отдельности:

* **разбор фразы** — «напомни позвонить маме завтра в девять»;
* **список** — человек спрашивает, и должен получить перечень, а не
  «напоминаний нет», когда они есть;
* **отмена** — «отмени напоминание позвонить маме» снимает именно то,
  что человек назвал.

Список хранится в состоянии плагина, сроки — в ядре. Разные места по
разным причинам: ядро знает, когда сработает, но не знает, что человек
хотел; состояние знает текст, но не знает времени.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_PLUGIN = pathlib.Path(r"D:\Sinaps\plugins\reminder\plugin.py")


@pytest.fixture
def reminder():
    spec = importlib.util.spec_from_file_location("reminder_plugin", _PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _State:
    def __init__(self, data=None):
        self.data = data or {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value


class _Ctx:
    """Контекст плагина: состояние, расписание, остаток."""

    def __init__(self, pending=None):
        self.state = _State()
        self.pending_data = pending or {}
        self.scheduled: list[tuple] = []
        self.cancelled: list[str] = []

    async def pending(self):
        return self.pending_data

    async def schedule(self, plan_id, delay_s, payload=None):
        self.scheduled.append((plan_id, delay_s, payload or {}))
        return 0.0

    async def cancel(self, plan_id):
        self.cancelled.append(plan_id)
        return True


# --- разбор фразы ------------------------------------------------------------


@pytest.mark.parametrize(("фраза", "что"), [
    ("позвонить маме завтра в девять", "позвонить маме"),
    ("выключить плиту через 10 минут", "выключить плиту"),
    ("оплатить интернет в понедельник в десять", "оплатить интернет"),
    ("позвонить в сервис", "позвонить в сервис"),
])
def test_what_is_separated_from_when(reminder, фраза, что):
    """Дело отделяется от времени.

    Напоминание без дела бессмысленно: «напомни в девять» — что
    напоминать-то? Человек назвал дело, и оно должно сохраниться
    дословно.
    """
    what, _when = reminder._split(фраза)

    assert what == что


# --- постановка --------------------------------------------------------------


@pytest.mark.asyncio
async def test_reminder_is_scheduled(reminder):
    """Напоминание уходит в расписание ядра."""
    ctx = _Ctx()

    reply = await reminder._set(ctx, {"text": "позвонить маме через 5 минут"})

    assert ctx.scheduled, "напоминание не поставилось"
    plan_id, delay, payload = ctx.scheduled[0]
    assert payload["text"] == "позвонить маме"
    assert 240 < delay < 360, f"срок подозрительный: {delay}"
    assert "Напомню" in reply.speech


@pytest.mark.asyncio
async def test_reminder_is_remembered(reminder):
    """Напоминание попадает в список.

    Ядро знает срок, но не знает, что человек хотел. Текст хранится в
    состоянии плагина — иначе «какие напоминания» не сможет ответить.
    """
    ctx = _Ctx()

    await reminder._set(ctx, {"text": "позвонить маме через 5 минут"})

    items = await ctx.state.get("reminders")
    assert items and items[0]["text"] == "позвонить маме"


@pytest.mark.asyncio
async def test_same_reminder_replaces_the_previous(reminder):
    """Повтор того же дела не плодит записи.

    Человек сказал «напомни позвонить маме», потом «напомни позвонить
    маме через час» — он переставил, а не добавил второе.
    """
    ctx = _Ctx()

    await reminder._set(ctx, {"text": "позвонить маме через 5 минут"})
    await reminder._set(ctx, {"text": "позвонить маме через час"})

    items = await ctx.state.get("reminders")
    assert len(items) == 1, items
    assert len(ctx.scheduled) == 2, "второе напоминание не поставилось"


@pytest.mark.asyncio
async def test_empty_reminder_is_refused(reminder):
    """Без дела напоминание не ставится."""
    ctx = _Ctx()

    reply = await reminder._set(ctx, {"text": "  "})

    assert not ctx.scheduled
    assert "не понял" in reply.speech.lower()


# --- список ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_is_empty_when_nothing(reminder):
    """Нет напоминаний — так и говорим."""
    ctx = _Ctx()

    reply = await reminder._list(ctx, {})

    assert "нет" in reply.speech.lower()


@pytest.mark.asyncio
async def test_list_shows_reminders_with_time(reminder):
    """Список показывает, что напомнить и сколько осталось."""
    ctx = _Ctx()
    await reminder._set(ctx, {"text": "позвонить маме через 5 минут"})
    ctx.pending_data = {ctx.scheduled[0][0]: 300}

    reply = await reminder._list(ctx, {})

    assert "позвонить маме" in reply.speech
    assert "5 минут" in reply.speech, reply.speech


# --- отмена ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_removes_named_reminder(reminder):
    """«Отмени напоминание позвонить маме» снимает именно его."""
    ctx = _Ctx()
    await reminder._set(ctx, {"text": "позвонить маме через 5 минут"})
    await reminder._set(ctx, {"text": "выключить плиту через 10 минут"})

    reply = await reminder._cancel(ctx, {"text": "позвонить маме"})

    assert ctx.cancelled, "напоминание не сняли"
    items = await ctx.state.get("reminders")
    assert len(items) == 1
    assert items[0]["text"] == "выключить плиту"
    assert "снято" in reply.speech.lower()


@pytest.mark.asyncio
async def test_cancel_unknown_says_so(reminder):
    """Такого напоминания нет — так и говорим."""
    ctx = _Ctx()

    reply = await reminder._cancel(ctx, {"text": "позвонить маме"})

    assert "нет" in reply.speech.lower()


# --- команды объявлены ------------------------------------------------------


def test_commands_are_declared(reminder):
    """Команды на месте: поставить, список, отменить и внутренний звонок."""
    import sys

    sys.path.insert(0, r"D:\Sinaps")
    from sinaps.skills.registry import SkillRegistry

    registry = SkillRegistry()
    reminder.register(registry)

    names = {spec.name for spec in registry.specs}

    assert "reminder.set" in names
    assert "reminder.list" in names
    assert "reminder.cancel" in names
    assert "reminder.on_fire" in names


@pytest.mark.parametrize("фраза", [
    "напомни позвонить маме завтра в девять",
    "напомнить выключить плиту через 10 минут",
    "поставь напоминание оплатить интернет в понедельник",
])
def test_phrases_are_recognised(reminder, фраза):
    """Фразы узнаются настоящим разбором речи."""
    import sys

    sys.path.insert(0, r"D:\Sinaps")
    from sinaps.skills.registry import SkillRegistry

    registry = SkillRegistry()
    reminder.register(registry)

    match = registry.match(фраза)

    assert match is not None, f"фраза не разобрана: {фраза!r}"
    assert match.spec.name == "reminder.set", match.spec.name


@pytest.mark.parametrize("фраза", [
    "какие напоминания",
    "что я напомнил",
    "список напоминаний",
    "мои напоминания",
])
def test_list_phrases_are_recognised(reminder, фраза):
    import sys

    sys.path.insert(0, r"D:\Sinaps")
    from sinaps.skills.registry import SkillRegistry

    registry = SkillRegistry()
    reminder.register(registry)

    match = registry.match(фраза)

    assert match is not None, f"фраза не разобрана: {фраза!r}"
    assert match.spec.name == "reminder.list", match.spec.name