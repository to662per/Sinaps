# -*- coding: utf-8 -*-
"""Напоминания: «завтра в девять позвонить маме».

Чем отличается от таймера. Таймер — это «через пять минут»: срок
короткий, и человек ждёт его, глядя на часы. Напоминание — это дело:
«позвонить маме», «забрать ребёнка», «оплатить интернет». У него есть
**что** напомнить, а не только **когда**.

Главное отличие — время. Таймер живёт минутами, напоминание может стоять
на завтра или на следующий понедельник. За это время ассистент
перезапустится, и напоминание должно пережить перезапуск: срок хранит
ядро (`core/scheduler.py`), а не плагин.

Список напоминаний хранится здесь же, в состоянии плагина: ядро знает
только сроки, а человеку нужен ещё и текст — «позвонить маме».

Чего нет намеренно: повторяющихся напоминаний («каждый вторник»).
Место в таблице сроков есть, но обещать их, пока они не сделаны,
нельзя.
"""

from __future__ import annotations

import re

from sinaps.core.when import human_duration, parse_when
from sinaps.skills.base import Response

#: Разделитель между «что» и «когда» в записи напоминания.
_SEP = "\x1f"

#: Ключ в состоянии плагина, где лежит список напоминаний.
_STATE_KEY = "reminders"


def _slug(text: str) -> str:
    """Имя срока из текста напоминания.

    Из текста, а не из счётчика: человек говорит «отмени напоминание
    позвонить маме» — и это должно снимать именно то, что он назвал.
    Счётчик после перезапуска начался бы с нуля, и второе напоминание
    затёрло бы первое.
    """
    cleaned = re.sub(r"[^\w\-]+", "-", (text or "").strip().lower(),
                     flags=re.UNICODE)
    cleaned = cleaned.strip("-")
    return (cleaned or "zvonok")[:40]


def _split(text: str) -> tuple[str, str]:
    """Разделить фразу на «что напомнить» и «когда».

    Разделяет только тогда, когда вторая часть действительно похожа на
    время. Иначе «позвонить в сервис» разрезалось бы на «позвонить» и
    «в сервис», и человек получил бы напоминание не о том.
    """
    lowered = (text or "").strip().lower()

    for marker in (" послезавтра в ", " завтра в ", " сегодня в ",
                   " послезавтра ", " завтра ", " сегодня ", " через ", " в "):
        index = lowered.find(marker)
        if index <= 0:
            continue
        what = (text or "").strip()[:index].strip()
        when = (text or "").strip()[index:].strip()
        if what and parse_when(when) is not None:
            return what, when

    return (text or "").strip(), ""


async def _reminders(ctx) -> list[dict]:
    """Список напоминаний из состояния плагина."""
    try:
        raw = await ctx.state.get(_STATE_KEY)
    except Exception:                              # noqa: BLE001
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


async def _save_reminders(ctx, items: list[dict]) -> None:
    try:
        await ctx.state.set(_STATE_KEY, items)
    except Exception:                              # noqa: BLE001
        pass


async def _set(ctx, slots) -> Response:
    """Поставить напоминание."""
    phrase = str((slots or {}).get("text") or "").strip()
    if not phrase:
        return Response(speech="Что напомнить — не понял.")

    what, when_text = _split(phrase)
    target = parse_when(when_text) if when_text else None

    if target is None:
        # Нет времени — значит «через пять минут» от текущего момента.
        target = parse_when("через 5 минут")

    import time

    delay = max(1.0, target.timestamp() - time.time())

    plan_id = f"reminder-{_slug(what)}"
    await ctx.schedule(plan_id, delay, {"text": what})

    items = await _reminders(ctx)
    items = [i for i in items if i.get("id") != plan_id]
    items.append({"id": plan_id, "text": what,
                  "due": int(target.timestamp())})
    await _save_reminders(ctx, items)

    return Response(speech=f"Напомню: {what}. {_when_phrase(target)}")


def _when_phrase(target) -> str:
    """Когда сработает — словами."""
    import time

    left = int(max(0, target.timestamp() - time.time()))
    if left < 3600:
        return f"Через {human_duration(left)}."
    return "В назначенное время."


async def _list(ctx, slots) -> Response:
    """Какие напоминания есть."""
    items = await _reminders(ctx)
    if not items:
        return Response(speech="Напоминаний нет.")

    left = {}
    try:
        left = await ctx.pending()
    except Exception:                              # noqa: BLE001
        left = {}

    import time

    parts = []
    for item in items:
        seconds = int(left.get(item.get("id"), 0)) if isinstance(left, dict) else 0
        if seconds > 0:
            parts.append(f"{item.get('text')} — через "
                         f"{human_duration(seconds)}")
        else:
            parts.append(f"{item.get('text')} — скоро")
    return Response(speech="Напоминания: " + "; ".join(parts))


async def _cancel(ctx, slots) -> Response:
    """Снять напоминание."""
    phrase = str((slots or {}).get("text") or "").strip()
    if not phrase:
        return Response(speech="Какое напоминание снять — не понял.")

    wanted = _slug(phrase)
    items = await _reminders(ctx)
    remaining = []
    removed = False
    for item in items:
        if not removed and (item.get("id") == f"reminder-{wanted}"
                            or wanted in str(item.get("text", "")).lower()):
            await ctx.cancel(item.get("id"))
            removed = True
            continue
        remaining.append(item)

    if not removed:
        return Response(speech="Такого напоминания нет.")

    await _save_reminders(ctx, remaining)
    return Response(speech="Напоминание снято.")


async def _on_fire(ctx, slots) -> Response:
    """Ядро разбудило плагин в срок.

    Возвращает, что сказать, — показывает ядро. Изнутри обработчика
    ходить в ядро нельзя: главный цикл плагина занят этим обработчиком
    и не читает ввод, а обращение к возможности ждёт ответа именно
    оттуда.
    """
    text = str((slots or {}).get("text") or "Пора")
    return Response(speech=text)


def register(registry) -> None:
    """Объявить навыки напоминаний."""
    from sinaps.skills.base import Skill, SlotSpec

    skill = Skill("reminder", description="Напоминания: когда и о чём")

    skill.command(
        "set", _set,
        patterns=[r"^напомни\s+(?P<text>.+)$",
                  r"^напомнить\s+(?P<text>.+)$",
                  r"^(?:поставь|поставьте)\s+напоминани[ея]\s+(?P<text>.+)$"],
        slots={"text": SlotSpec(name="text", type="string", required=True,
                                description="Что напомнить и когда")},
        examples=["напомни позвонить маме завтра в девять",
                  "напомни через 10 минут выключить плиту"],
        description="Поставить напоминание.")

    skill.command(
        "list", _list,
        patterns=[r"^какие\s+(?:у\s+меня\s+)?напоминани[яе]$",
                  r"^что\s+я\s+напомнил$",
                  r"^список\s+напоминаний$",
                  r"^мои\s+напоминани[яе]$"],
        examples=["какие напоминания"],
        description="Показать напоминания.")

    skill.command(
        "cancel", _cancel,
        patterns=[r"^отмени\s+напоминани[ея]\s+(?P<text>.+)$"],
        slots={"text": SlotSpec(name="text", type="string", required=True,
                                description="Что отменить")},
        examples=["отмени напоминание позвонить маме"],
        description="Снять напоминание.")

    # Внутренняя команда: её зовёт ядро, когда срок настал. Человек
    # сказать её не может.
    skill.command(
        "on_fire", _on_fire,
        patterns=[r"(?!)"],
        examples=[],
        description="Внутренняя команда: ядро зовёт плагин в срок.")

    registry.add(skill)