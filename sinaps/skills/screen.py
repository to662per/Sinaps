"""Навыки экрана, окон и буфера обмена."""

from __future__ import annotations

import time
from pathlib import Path

from ..skills.base import Response, Skill, SlotSpec


def register() -> Skill:
    skill = Skill("screen", description="Экран и окна")

    async def screenshot(ctx, slots):  # noqa: ANN001
        try:
            path = ctx.platform.screenshot()
        except (NotImplementedError, RuntimeError) as exc:
            return Response(speech="Не могу сделать скриншот", display=str(exc))
        except Exception:
            return Response(speech="Не получилось сделать скриншот")
        return Response(speech="Скриншот готов", display=path)

    async def brightness(ctx, slots):  # noqa: ANN001
        level = slots.get("level")
        if level is None:
            return Response(speech="Не понял, какую яркость поставить")
        ok = ctx.platform.set_brightness(int(level))
        if not ok:
            return Response(
                speech="Яркость меняю только на ноутбуках",
                display="на этом компьютере нет подсветки",
            )
        return Response(speech=f"Яркость {int(level)} процентов")

    skill.command(
        "screenshot",
        screenshot,
        patterns=[
            r"^(?:сделай|сними|сделай) скриншот$",
            r"^скриншот$",
            r"^снимок экрана$",
            r"^screenshot$",
        ],
        examples=["сделай скриншот", "снимок экрана"],
        description="Сделать скриншот экрана",
        priority=10,
    )

    skill.command(
        "brightness",
        brightness,
        patterns=[
            r"^(?:яркость|яркость экрана) (?:на|до|поставь)\s+(?P<level>\d+)\s*(?:процентов|%)?$",
            r"^сделай яркость (?P<level>\d+)$",
            r"^убавь яркость$|^прибавь яркость$",
        ],
        examples=["яркость на 50", "сделай яркость 80"],
        slots={"level": SlotSpec("level", "number", required=False,
                                 description="Яркость в процентах")},
        description="Изменить яркость экрана",
        priority=10,
    )

    return skill


def register_clipboard() -> Skill:
    skill = Skill("clipboard", description="Буфер обмена")

    async def copy(ctx, slots):  # noqa: ANN001
        text = slots.get("text")
        if not text:
            return Response(speech="Скажи, что скопировать")
        if not ctx.platform.clipboard_set(text):
            return Response(speech="Не смог скопировать")
        return Response(speech="Скопировал")

    async def paste(ctx, slots):  # noqa: ANN001
        selection = ctx.platform.clipboard_get()
        if not selection.available:
            return Response(speech="Не могу прочитать буфер обмена")
        return Response(
            speech="Вот что в буфере обмена",
            display=selection.text or "(пусто)",
        )

    skill.command(
        "copy",
        copy,
        patterns=[
            r"^(?:скопируй|запомни)\s+(?P<text>.+)$",
            r"^copy\s+(?P<text>.+)$",
        ],
        examples=["скопируй мой пароль", "запомни номер телефона"],
        slots={"text": SlotSpec("text", "string", min_length=1,
                                description="Текст для копирования")},
        description="Скопировать текст в буфер обмена",
        priority=10,
    )

    skill.command(
        "paste",
        paste,
        patterns=[
            r"^(?:что в буфере|буфер обмена|прочитай буфер)$",
        ],
        examples=["что в буфере", "буфер обмена"],
        description="Показать содержимое буфера обмена",
        priority=10,
    )

    return skill


def register_windows() -> Skill:
    skill = Skill("windows", description="Окна")

    async def list_windows(ctx, slots):  # noqa: ANN001
        windows = ctx.platform.list_windows()
        if not windows:
            return Response(
                speech="Не вижу открытых окон",
                display="возможно, нет доступа или всё свёрнуто",
            )
        titles = ", ".join(w.title for w in windows[:8])
        return Response(
            speech=f"Открыто окон: {len(windows)}",
            display=titles,
        )

    async def focus_window(ctx, slots):  # noqa: ANN001
        query = slots["title"].lower()
        windows = ctx.platform.list_windows()
        target = next((w for w in windows if query in w.title.lower()), None)
        if not target:
            return Response(speech=f"Не нашёл окно «{slots['title']}»")
        if not ctx.platform.focus_window(target.handle):
            return Response(
                speech="Не смог переключиться на окно",
                display="Windows запрещает смену фокуса без разрешения",
            )
        return Response(speech="Переключился", display=target.title)

    async def close_window(ctx, slots):  # noqa: ANN001
        query = slots["title"].lower()
        windows = ctx.platform.list_windows()
        target = next((w for w in windows if query in w.title.lower()), None)
        if not target:
            return Response(speech=f"Не нашёл окно «{slots['title']}»")
        ctx.platform.close_window(target.handle)
        return Response(speech="Закрываю окно", display=target.title)

    async def minimize_all(ctx, slots):  # noqa: ANN001
        ok = ctx.platform.minimize_all()
        return Response(speech="Сворачиваю всё" if ok
                        else "Не получилось свернуть окна")

    skill.command(
        "list",
        list_windows,
        patterns=[
            r"^(?:какие окна|список окон|открытые окна|что открыто)$",
            r"^(?:какие|список)\s+окн\w+(?:\s+открыт\w*)?$",
            r"^что открыто(?: на экране)?$",
        ],
        examples=["какие окна открыты", "список окон"],
        description="Показать открытые окна",
        priority=10,
    )

    skill.command(
        "focus",
        focus_window,
        patterns=[
            r"^(?:переключись на|открой окно|покажи окно)\s+(?P<title>.+)$",
            r"^focus on\s+(?P<title>.+)$",
        ],
        examples=["переключись на блокнот", "покажи окно браузера"],
        slots={"title": SlotSpec("title", "string", min_length=1,
                                 description="Название окна")},
        description="Переключиться на окно",
        priority=10,
    )

    skill.command(
        "close",
        close_window,
        patterns=[
            # Только с явным словом «окно»: иначе «закрой блокнот» уходило
            # сюда вместо закрытия программы.
            r"^закрой окно\s+(?P<title>.+)$",
            r"^закрой это окно$",
        ],
        examples=["закрой окно блокнот"],
        slots={"title": SlotSpec("title", "string", min_length=1,
                                 description="Название окна")},
        description="Закрыть окно",
        priority=11,
    )

    skill.command(
        "minimize_all",
        minimize_all,
        patterns=[
            r"^(?:сверни всё|сверни все окна|сверни окна)$",
        ],
        examples=["сверни всё", "сверни все окна"],
        description="Свернуть все окна",
        priority=10,
    )

    return skill
