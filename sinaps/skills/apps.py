"""Навыки работы с программами и файлами."""

from __future__ import annotations

from ..skills.base import Response, Skill, SlotSpec


def register() -> Skill:
    skill = Skill("apps", description="Программы и файлы")

    def _not_found(what: str, query: str) -> str:
        """Ответ «не нашёл» с подсказками — как у программ.

        Раньше файлы и папки отвечали голым «Не нашёл», хотя рядом был
        «Отчёт2026.pdf», а человек сказал «отчет». Программы в том же
        случае предлагали варианты, и разница выглядела случайной.
        """
        try:
            from ..platform.windows import suggest_paths

            similar = suggest_paths(query, limit=3)
        except Exception:                             # noqa: BLE001
            similar = []

        if similar:
            names = ", ".join(similar)
            return (f"Не нашёл {what} «{query}». Похожее: {names}. "
                    f"Скажи точнее.")
        return f"Не нашёл {what} «{query}»"

    async def open_app(ctx, slots):  # noqa: ANN001
        query = slots["name"]
        ok, info = ctx.platform.launch_app(query)
        if ok:
            return Response(speech=f"Открываю {query}", display=f"запуск: {info}")
        return Response(speech=info, display=f"не найдено: {query}")

    async def close_app(ctx, slots):  # noqa: ANN001
        # Убийство процесса — грубая операция, но пользователь просил явно.
        query = slots["name"].lower()
        killed = _kill_process(query)
        if not killed:
            return Response(speech=f"Не нашёл, что закрыть: {query}")
        plural = "процесс" if killed == 1 else "процесса"
        if 5 <= killed <= 20:
            plural = "процессов"
        return Response(speech=f"Закрыл {killed} {plural}", display=killed)

    async def list_programs(ctx, slots):  # noqa: ANN001
        apps = ctx.platform.list_apps()
        return Response(
            speech=f"Умею открывать {len(apps)} программ",
            display=", ".join(a.name for a in apps[:40]),
        )

    async def open_file(ctx, slots):  # noqa: ANN001
        query = slots["name"]
        ok = ctx.platform.open_path(query)
        if ok:
            return Response(speech=f"Открываю {query}")
        return Response(speech=_not_found("файл", query),
                        display=f"не найдено: {query}")

    async def open_folder(ctx, slots):  # noqa: ANN001
        query = slots["name"]
        ok = ctx.platform.open_path(query)
        if ok:
            return Response(speech=f"Открываю папку {query}")
        return Response(speech=_not_found("папку", query),
                        display=f"не найдено: {query}")

    async def show_folder(ctx, slots):  # noqa: ANN001
        query = slots["name"]
        ok = ctx.platform.open_path(query, reveal=True)
        if not ok:
            return Response(speech=f"Не нашёл {query}")
        return Response(speech="Показываю в проводнике")

    skill.command(
        "open",
        open_app,
        patterns=[
            r"^(?:открой|открыть|запусти|запустить|включи)\s+(?P<name>.+)$",
            r"^launch\s+(?P<name>.+)$",
            r"^open\s+(?P<name>.+)$",
        ],
        examples=["открой блокнот", "запусти калькулятор", "включи диспетчер задач"],
        slots={"name": SlotSpec("name", "string", min_length=1,
                                description="Название программы")},
        description="Открыть программу",
        priority=5,
    )

    skill.command(
        "close",
        close_app,
        patterns=[
            r"^(?:закрой|закрыть|выключи|убий)\s+(?:программу\s+|приложение\s+)?(?P<name>.+)$",
            r"^close\s+(?P<name>.+)$",
        ],
        examples=["закрой браузер", "выключи программу steam"],
        slots={"name": SlotSpec("name", "string", min_length=1,
                                description="Название программы")},
        description="Закрыть программу",
        priority=6,
    )

    skill.command(
        "list",
        list_programs,
        patterns=[
            r"^(?:какие программы|что у меня установлено|список программ)(?: доступны)?$",
            r"^что ты умеешь открывать$",
            # Длинные формы с «покажи». Раньше фраза «покажи список
            # доступных программ» не подходила ни под один шаблон, и
            # ассистент отвечал «не понял» — хотя спросили именно об этом.
            r"^(?:покажи|показать|пок��жи|список)\s+"
            r"(?:список\s+)?(?:доступных\s+|всех\s+)?(?:программ|приложений)$",
            r"^(?:какие|сколько)\s+(?:доступные\s+)?"
            r"(?:программы|приложения|программ|приложений)"
            r"(?:\s+(?:доступны|доступно|установлены|установлено|есть))?$",
        ],
        examples=["какие программы доступны",
                  "что ты умеешь открывать",
                  "покажи список доступных программ"],
        description="Показать список доступных программ",
        priority=10,
    )

    skill.command(
        "open_file",
        open_file,
        patterns=[
            r"^открой файл\s+(?P<name>.+)$",
            r"^open file\s+(?P<name>.+)$",
        ],
        examples=["открой файл отчёт", "открой файл диплом"],
        slots={"name": SlotSpec("name", "path", min_length=1,
                                description="Имя или путь к файлу")},
        description="Открыть файл",
        priority=8,
    )

    skill.command(
        "open_folder",
        open_folder,
        patterns=[
            r"^открой папку\s+(?P<name>.+)$",
            r"^покажи папку\s+(?P<name>.+)$",
        ],
        examples=["открой папку загрузки", "открой папку документы"],
        slots={"name": SlotSpec("name", "path", min_length=1,
                                description="Имя или путь к папке")},
        description="Открыть папку",
        priority=8,
    )

    skill.command(
        "reveal",
        show_folder,
        patterns=[
            r"^покажи (?:в проводнике|где находится)\s+(?P<name>.+)$",
        ],
        examples=["покажи в проводнике отчёт"],
        slots={"name": SlotSpec("name", "path", min_length=1,
                                description="Имя или путь")},
        description="Показать файл в проводнике",
        priority=9,
    )

    return skill


def _kill_process(query: str) -> int:
    """Закрывает процессы по имени. Возвращает количество закрытых."""
    import psutil

    killed = 0
    needle = query.lower().replace(".exe", "").strip()
    if not needle:
        return 0

    own_pid = psutil.Process().pid

    for process in psutil.process_iter(["pid", "name"]):
        try:
            info = process.info
            name = (info.get("name") or "").lower().replace(".exe", "")
            # Свой процесс и критичные службы не трогаем.
            if info.get("pid") == own_pid or not name:
                continue
            if needle in name and name not in _PROTECTED:
                process.terminate()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return killed


#: Процессы, которые нельзя закрывать голосовой командой.
_PROTECTED = {
    "system", "csrss", "wininit", "winlogon", "services", "lsass",
    "svchost", "smss", "winhttp", "dwm", "explorer", "sinaps", "python",
    "pythonw", "powershell", "cmd",
}
