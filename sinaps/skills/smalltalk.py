"""Навыки общения и справки.

«Что ты умеешь» здесь — лучший пример пользы единой декларации: список
команд генерируется из реестра навыков, поэтому справка не может разойтись
с реальными возможностями. Забыть добавить команду в справку невозможно.
"""

from __future__ import annotations

from ..skills.base import Response, Skill


def register(registry=None) -> Skill:
    skill = Skill("smalltalk", description="Общение")

    async def greeting(ctx, slots):  # noqa: ANN001
        return Response(
            speech=f"Привет, {ctx.user_name}! Я Синапс.",
            display=f"приветствие -> {ctx.user_name}",
        )

    async def farewell(ctx, slots):  # noqa: ANN001
        return Response(speech=f"До встречи, {ctx.user_name}!")

    async def thanks(ctx, slots):  # noqa: ANN001
        return Response(speech="Всегда пожалуйста")

    async def who_are_you(ctx, slots):  # noqa: ANN001
        total = len(registry) if registry else 0
        return Response(
            speech=f"Я Синапс, твой голосовой помощник. Умею {total} команд.",
            display=f"навыков: {total}",
        )

    async def capabilities(ctx, slots):  # noqa: ANN001
        if registry is None:
            return Response(speech="Не знаю, что умею")
        # Голосом длинный список не пересказать — отдаём кратко и подробно на экран.
        groups = {}
        for spec in registry.specs:
            if spec.hidden:
                continue
            group = spec.name.split(".")[0]
            groups.setdefault(group, 0)
            groups[group] += 1

        parts = ", ".join(f"{g} — {c}" for g, c in sorted(groups.items()))
        return Response(
            speech=f"Умею {sum(groups.values())} команд: {parts}. Подробности на экране.",
            display=registry.help_text(),
        )

    skill.command(
        "greeting",
        greeting,
        patterns=[
            r"^(?:привет|здравствуй|здравствуйте|хай|доброе утро|добрый день|добрый вечер)$",
            r"^(?:привет|здравствуй)\s+(?P<name>[\w\s-]{1,30})$",
            r"^hi$|^hello$",
        ],
        examples=["привет", "здравствуй", "добрый день"],
        slots={"name": SlotSpec_name()},
        description="Поздороваться",
        priority=15,
    )

    skill.command(
        "farewell",
        farewell,
        patterns=[
            r"^(?:пока|до свидания|до встречи|прощай|спокойной ночи)$",
            r"^bye$",
        ],
        examples=["пока", "до свидания"],
        description="Попрощаться",
        priority=15,
    )

    skill.command(
        "thanks",
        thanks,
        patterns=[
            r"^(?:спасибо|благодарю|спс|пасиб)$",
            r"^thanks$",
        ],
        examples=["спасибо", "благодарю"],
        description="Поблагодарить",
        priority=15,
    )

    skill.command(
        "identity",
        who_are_you,
        patterns=[
            r"^кто ты(?: такой)?$",
            r"^как тебя зовут$",
            r"^что ты такое$",
            r"^who are you$",
        ],
        examples=["кто ты", "как тебя зовут"],
        description="Рассказать о себе",
        priority=15,
    )

    skill.command(
        "capabilities",
        capabilities,
        patterns=[
            r"^(?:что ты умеешь|что умеешь|какие команды|какие у тебя команды)$",
            r"^помощь$|^help$",
            r"^что ты можешь$",
        ],
        examples=["что ты умеешь", "какие команды", "помощь"],
        description="Показать список команд",
        priority=15,
    )

    return skill


def SlotSpec_name():  # noqa: N802
    """Слот имени — создаётся лениво, чтобы не тянуть импорт в шапку."""
    from ..skills.base import SlotSpec

    return SlotSpec("name", "string", required=False,
                    description="Имя пользователя")
