"""Навыки звука и мультимедиа."""

from __future__ import annotations

from ..nlu.verbs import verb, verb_with
from ..skills.base import Response, Skill, SlotSpec

#: На сколько процентов меняется громкость, когда число не названо.
#: Раньше здесь стояло число нажатий клавиши — 5. Одно нажатие сдвигает
#: уровень ровно на 2 процента (шаг Windows 50 из 100, замерено),
#: поэтому голос объявлял «на 5 ступеней», а громкость уезжала на 10.
#: Теперь единица одна — проценты, их и объявляем.
_STEP = 10
_MAX_PERCENT = 100


def register() -> Skill:
    skill = Skill("media", description="Звук и мультимедиа")

    async def volume_up(ctx, slots):  # noqa: ANN001
        percent = _delta(slots)
        before = _level(ctx)
        if not ctx.platform.volume_up(percent):
            return Response(speech="Не могу изменить громкость")
        return Response(speech=_shifted(ctx, "громче", before, percent, up=True))

    async def volume_down(ctx, slots):  # noqa: ANN001
        percent = _delta(slots)
        before = _level(ctx)
        if not ctx.platform.volume_down(percent):
            return Response(speech="Не могу изменить громкость")
        return Response(speech=_shifted(ctx, "тише", before, percent, up=False))

    async def volume_mute(ctx, slots):  # noqa: ANN001
        if not ctx.platform.volume_mute():
            return Response(speech="Не могу переключить звук")
        return Response(speech="Переключаю звук")

    async def volume_level(ctx, slots):  # noqa: ANN001
        state = ctx.platform.volume_state()
        if not state.supported:
            return Response(speech="Не могу узнать уровень громкости")
        if state.percent is None:
            # Честнее сказать «не знаю», чем назвать случайное число.
            return Response(
                speech="Точный уровень громкости я не измеряю",
                display="поддерживается только увеличение, уменьшение и отключение",
            )
        if state.muted:
            return Response(speech=f"Звук выключен, громкость {state.percent} процентов")
        return Response(speech=f"Громкость {state.percent} процентов")

    async def play_pause(ctx, slots):  # noqa: ANN001
        ok = ctx.platform.media_play_pause()
        return Response(speech="Переключаю воспроизведение" if ok
                        else "Не управляю медиаплеером")

    async def next_track(ctx, slots):  # noqa: ANN001
        ok = ctx.platform.media_next()
        return Response(speech="Следующий трек" if ok else "Не управляю медиаплеером")

    async def prev_track(ctx, slots):  # noqa: ANN001
        ok = ctx.platform.media_previous()
        return Response(speech="Предыдущий трек" if ok else "Не управляю медиаплеером")

    skill.command(
        "volume_up",
        volume_up,
        patterns=[
            # «громче»
            rf"^(?:{verb_with('', 'сделать', 'увеличить', 'прибавить', 'добавить')} )?громче$",
            rf"^(?:{verb_with('', 'сделать', 'увеличить', 'прибавить', 'добавить')} )?громче\s+(?:на\s+)?(?P<percent>.+)$",
            # «прибавь громкость» — глагол и существительное в разном порядке
            rf"^(?:{verb('прибавить', 'добавить', 'увеличить', 'сделать')})\s+громкость(?:\s+(?:на\s+)?(?P<percent>.+))?$",
            r"^громкость (?:повыше|больше|увеличить)$",
            r"^подними громкость$",
            r"^volume up$",
        ],
        examples=["сделай громче", "прибавь громкость", "громче на десять"],
        slots={"percent": SlotSpec("percent", "number", required=False,
                                   default=_STEP, description="На сколько процентов")},
        description="Увеличить громкость",
        priority=10,
    )

    skill.command(
        "volume_down",
        volume_down,
        patterns=[
            rf"^(?:{verb_with('', 'сделать', 'уменьшить', 'убрать', 'сбавить')} )?тише$",
            rf"^(?:{verb_with('', 'сделать', 'уменьшить', 'убрать', 'сбавить')} )?тише\s+(?:на\s+)?(?P<percent>.+)$",
            rf"^(?:{verb('убрать', 'уменьшить', 'сделать', 'сбавить')})\s+громкость(?:\s+(?:на\s+)?(?P<percent>.+))?$",
            r"^громкость (?:пониже|меньше|уменьшить)$",
            r"^убавь громкость$",
            r"^volume down$",
        ],
        examples=["сделай тише", "убавь громкость", "тише на пять"],
        slots={"percent": SlotSpec("percent", "number", required=False,
                                   default=_STEP, description="На сколько процентов")},
        description="Уменьшить громкость",
        priority=10,
    )

    skill.command(
        "mute",
        volume_mute,
        patterns=[
            rf"^(?:{verb('выключить', 'отключить', 'заглушить')}) звук$",
            rf"^(?:{verb('включить', 'возобновить')}) звук$",
            r"^mute$",
        ],
        examples=["выключи звук", "отключи звук"],
        description="Отключить или включить звук",
        priority=10,
    )

    skill.command(
        "level",
        volume_level,
        patterns=[
            r"^(?:какая |какой )?(?:уровень |текущий уровень )?громкости$",
            r"^какая громкость$",
        ],
        examples=["какая громкость", "какой уровень громкости"],
        description="Узнать уровень громкости",
        priority=10,
    )

    skill.command(
        "play_pause",
        play_pause,
        patterns=[
            r"^(?:пауза|play|play ?pause|воспроизвести|play\/pause)$",
            rf"^(?:{verb('продолжить', 'возобновить')})(?: воспроизведение| музыку)?$",
        ],
        examples=["пауза", "продолжи", "play"],
        description="Поставить паузу или продолжить воспроизведение",
        priority=10,
    )

    skill.command(
        "next",
        next_track,
        patterns=[
            r"^(?:следующий трек|следующая песня|далее|next)$",
        ],
        examples=["следующий трек", "далее"],
        description="Переключить на следующий трек",
        priority=10,
    )

    skill.command(
        "previous",
        prev_track,
        patterns=[
            r"^(?:предыдущий трек|предыдущая песня|назад|previous)$",
        ],
        examples=["предыдущий трек", "назад"],
        description="Вернуть предыдущий трек",
        priority=10,
    )

    return skill


def _delta(slots: dict) -> int:
    """На сколько процентов сдвинуть громкость.

    Значение из голоса, а не число нажатий: «громче на пять» человек
    понимает как пять процентов, и именно столько он получает.
    """
    raw = slots.get("percent")
    if raw is None:
        return _STEP
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return _STEP
    return max(1, min(value, _MAX_PERCENT))


def _level(ctx) -> int | None:
    """Уровень до изменения — чтобы сказать не «сделал», а сколько стало.

    Если прочитать не вышло, `None` и ответ остаётся прежним: называть
    выдуманное число хуже, чем не называть никакого.
    """
    try:
        state = ctx.platform.volume_state()
    except Exception:  # noqa: BLE001 — чтение не должно ронять команду
        return None
    return state.percent if state.supported else None


def _shifted(ctx, word: str, before: int | None, percent: int, up: bool) -> str:
    """Ответ о смене громкости: что сделано и сколько стало.

    Старый ответ объявлял число нажатий («на 5 ступеней»), тогда как
    уровень уезжал на 10 процентов — шаг клавиши равен двум
    процентам. Человек слышал 5 и видел 10 и решал, что ассистент
    считает неверно. Теперь единица одна: проценты.

    Итоговый уровень не вычисляется наперёд, а читается после
    изменения. Разница видна у края: «сделай громче на 30» при 90
    даёт не 120, а 100, и предсказание об этом не знает.
    """
    from ..nlu.slots import _plural

    change = _plural(percent, "процент", "процента", "процентов")
    after = _level(ctx)

    if after is None:
        # Уровень прочитать не вышло — говорим только о намерении и
        # величине, но не выдумываем результат.
        return f"Делаю {word}, на {change}"

    if before is not None and after == before:
        return f"Громкость уже {after} процентов"

    moved = abs(after - before) if before is not None else percent
    if moved != percent:
        # Край диапазона: просили больше, чем оставалось.
        return f"Делаю {word}. Теперь {after} процентов"

    return f"Делаю {word}, на {change}. Теперь {after} процентов"
