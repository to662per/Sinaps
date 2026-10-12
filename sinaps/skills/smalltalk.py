"""Навыки общения и справки.

«Что ты умеешь» здесь — лучший пример пользы единой декларации: список
команд генерируется из реестра навыков, поэтому справка не может разойтись
с реальными возможностями. Забыть добавить команду в справку невозможно.

Но генерация правильного текста — только полдела. Раньше ассистент
обещал «подробности на экране», а `display` уходил только в консоль,
которой в режиме трея нет. Человек слышал обещание и не получал ничего.
Поэтому ответ теперь ещё и открывает панель возможностей — то, что он
действенно показывает.
"""

from __future__ import annotations

from . import taxonomy
from .base import Response, Skill


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

        groups: dict[str, int] = {}
        for spec in registry.specs:
            if spec.hidden:
                continue
            key = taxonomy.group_of(spec.name)
            groups[key] = groups.get(key, 0) + 1

        total = sum(groups.values())
        # Ходовые группы — по убыванию количества команд. Порядок из
        # таксономии тут не годится: человек слышит то, чем пользуется.
        top = [k for k, _ in sorted(groups.items(), key=lambda kv: -kv[1])]
        # Группа «Разговор» — это приветствия и «кто ты». Перечислять её
        # рядом с громкостью и программами обидно: человек услышит
        # «умею поговорить» и решит, что ему предлагают поболтать.
        spoken = [k for k in top if k != "smalltalk"]

        # Раньше здесь стояло «Подробности на экране». Экран появлялся
        # только в консоли, а в режиме трея её нет — ассистент обещал
        # то, чего не было. Теперь обещаем ровно то, что делаем:
        # открываем панель, и это видно.
        return Response(
            speech=(f"Умею {total} команд. Чаще всего это "
                    f"{taxonomy.speech_names(spoken)}. "
                    f"Открыл панель с примерами."),
            display=registry.help_text(),
            show_panel=True,
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
