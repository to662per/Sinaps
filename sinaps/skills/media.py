"""Навыки звука и мультимедиа."""

from __future__ import annotations

from ..nlu.verbs import verb, verb_with
from ..skills.base import Response, Skill, SlotSpec

#: Сколько нажатий на одну ступень. У Windows шаг громкости — 2 из 100,
#: поэтому «громче» удобно считать в шагах по 5.
_STEP = 5
_MAX_STEPS = 10


def register() -> Skill:
    skill = Skill("media", description="Звук и мультимедиа")

    async def volume_up(ctx, slots):  # noqa: ANN001
        steps = _steps(slots)
        if not ctx.platform.volume_up(steps):
            return Response(speech="Не могу изменить громкость")
        return Response(speech=f"Делаю громче, на {_step_word(steps)}")

    async def volume_down(ctx, slots):  # noqa: ANN001
        steps = _steps(slots)
        if not ctx.platform.volume_down(steps):
            return Response(speech="Не могу изменить громкость")
        return Response(speech=f"Делаю тише, на {_step_word(steps)}")

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
            rf"^(?:{verb_with('', 'сделать', 'увеличить', 'прибавить', 'добавить')} )?громче\s+(?:на\s+)?(?P<steps>.+)$",
            # «прибавь громкость» — глагол и существительное в разном порядке
            rf"^(?:{verb('прибавить', 'добавить', 'увеличить', 'сделать')})\s+громкость(?:\s+(?:на\s+)?(?P<steps>.+))?$",
            r"^громкость (?:повыше|больше|увеличить)$",
            r"^подними громкость$",
            r"^volume up$",
        ],
        examples=["сделай громче", "прибавь громкость", "громче на пять"],
        slots={"steps": SlotSpec("steps", "number", required=False, default=_STEP,
                                 description="На сколько ступеней")},
        description="Увеличить громкость",
        priority=10,
    )

    skill.command(
        "volume_down",
        volume_down,
        patterns=[
            rf"^(?:{verb_with('', 'сделать', 'уменьшить', 'убрать', 'сбавить')} )?тише$",
            rf"^(?:{verb_with('', 'сделать', 'уменьшить', 'убрать', 'сбавить')} )?тише\s+(?:на\s+)?(?P<steps>.+)$",
            rf"^(?:{verb('убрать', 'уменьшить', 'сделать', 'сбавить')})\s+громкость(?:\s+(?:на\s+)?(?P<steps>.+))?$",
            r"^громкость (?:пониже|меньше|уменьшить)$",
            r"^убавь громкость$",
            r"^volume down$",
        ],
        examples=["сделай тише", "убавь громкость", "тише на пять"],
        slots={"steps": SlotSpec("steps", "number", required=False, default=_STEP,
                                 description="На сколько ступеней")},
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


def _steps(slots: dict) -> int:
    """Ступени из слота, с защитой от абсурдных значений."""
    raw = slots.get("steps")
    if raw is None:
        return _STEP
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return _STEP
    return max(1, min(value, _MAX_STEPS))


def _step_word(steps: int) -> str:
    from ..nlu.slots import _plural

    return _plural(steps, "ступень", "ступени", "ступеней")
