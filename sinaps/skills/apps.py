"""Навыки работы с программами и файлами."""

from __future__ import annotations

from ..nlu.verbs import verb, verb_with
from ..skills.base import Response, Skill, SlotSpec


def register() -> Skill:
    skill = Skill("apps", description="Программы и файлы")

    def _not_found(what: str, query: str) -> str:
        """Ответ «не нашёл» с вариантами и подсказкой к следующему шагу.

        Раньше файлы и папки отвечали голым «Не нашёл», хотя рядом был
        «Отчёт2026.pdf», а человек сказал «отчет». Программы в том же
        случае предлагали варианты, и разница выглядела случайной.

        Варианты перечисляются, чтобы человек мог сказать нужное имя
        следующим шагом, а не догадываться, где ошибся.
        """
        try:
            from ..platform.windows import suggest_paths

            similar = suggest_paths(query, limit=3)
        except Exception:                             # noqa: BLE001
            similar = []

        if not similar:
            return f"Не нашёл {what} «{query}»"

        names = ", ".join(similar)
        return (f"Не нашёл {what} «{query}». Похожее: {names}. "
                f"Скажи «открыть {similar[0]}».")

    async def open_app(ctx, slots):  # noqa: ANN001
        query = slots["name"]
        ok, info = ctx.platform.launch_app(query)
        if ok:
            return Response(speech=f"Открываю {query}", display=f"запуск: {info}")
        return Response(speech=info, display=f"не найдено: {query}")

    async def close_app(ctx, slots):  # noqa: ANN001
        query = slots["name"].strip()
        if not query:
            return Response(speech="Скажи, что закрыть")

        # Сначала окна: WM_CLOSE даёт приложению шанс спросить
        # «сохранить изменения» и закрыться штатно. terminate() этого
        # не умеет — несохранённый текст пропадал бы молча.
        #
        # Порядок именно такой: окно мы закрываем, а если окна нет —
        # значит это фоновая программа, и terminate() единственный
        # способ.
        closed, save_warning = _close_windows(ctx.platform, query)
        if closed:
            return Response(
                speech=save_warning,
                display=f"окон закрыто: {closed}",
            )

        # Окна не нашлись: у процесса их может не быть вовсе
        # (служба, фоновая утилита). Тогда — terminate.
        target = _resolve(ctx.platform, query)
        killed = _kill_process(target) if target else 0
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
            rf"^(?:{verb('открыть', 'запустить', 'включить')})\s+(?P<name>.+)$",
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
            rf"^(?:{verb('закрыть', 'выключить')}|убий)\s+"
            r"(?:программу\s+|приложение\s+)?(?P<name>.+)$",
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


def _resolve(platform, query: str) -> str:
    """Имя процесса, которое человек назвал голосом.

    Просит у платформы: таблица русских названий одна на запуск и на
    закрытие. Раньше закрытие её не знало и искало буквальную
    подстроку — `'блокнот' in 'notepad'` ложно, поэтому «закрой
    блокнот» всегда отвечал «не нашёл».
    """
    try:
        return platform.resolve_process_name(query)
    except Exception:
        return ""


def _matches(candidate: str, target: str) -> bool:
    """Точное совпадение имени, а не подстрока.

    Подстрока опасна: «закрой блок» закрыл бы и `blender`, и `notepad`
    разом, а «закрой chrome» — ещё и `chromecast`. Подсказка может
    быть нечёткой, действие — нет.
    """
    if not candidate or not target:
        return False
    return candidate == target


#: Заголовки окон, которые нельзя закрывать.
#:
#: «Program Manager» — это окно рабочего стола, а не программа. Имя
#: его процесса — `explorer`, то есть совпадает с «закрой проводник».
#: Без этой проверки голосовая команда закрывала бы рабочий стол
#: Windows целиком.
_DESKTOP_TITLES = {"program manager", "рабочий стол", "desktop"}


def _is_desktop_shell(window) -> bool:  # noqa: ANN001
    return (window.title or "").strip().lower() in _DESKTOP_TITLES


def _close_windows(platform, query: str) -> tuple[int, str]:
    """Закрыть окна приложения через WM_CLOSE.

    Возвращает (сколько закрыто, что сказать). Окно ищется по имени
    ПРОЦЕССА, а не по заголовку: у Блокнота с открытым файлом
    заголовок — «sinaps.log — Блокнот», и имя программы из него не
    извлечь. Пользователь просил закрыть Блокнот — закрываем Блокнот,
    а не файл.
    """
    target = _resolve(platform, query)
    if not target:
        return 0, ""

    try:
        windows = platform.list_windows()
    except Exception:
        return 0, ""

    # Окна скрытые тоже считаем: приложение может быть свёрнуто в
    # трей, и человек всё равно считает его открытым.
    #
    # Оболочку рабочего стола пропускаем обязательно: у неё имя
    # процесса тоже `explorer`, а заголовок — «Program Manager». Её
    # закрытие уронит рабочий стол Windows. Проверка идёт по заголовку
    # и по имени класса окна, потому что переименовать «Program
    # Manager» нельзя, а вот подделать заголовок обычного окна легко —
    # и вот тогда проверка по имени класса спасёт.
    handles = [
        w.handle for w in windows
        if _matches((w.process or "").lower().replace(".exe", ""), target)
        and not _is_desktop_shell(w)
    ]
    if not handles:
        return 0, ""

    closed = 0
    for handle in handles:
        try:
            if platform.close_window(handle):
                closed += 1
        except Exception:
            continue

    if not closed:
        return 0, ""

    word = "окно" if closed == 1 else "окна" if closed < 5 else "окон"
    return closed, f"Закрыл {closed} {word}"


def _kill_process(target: str) -> int:
    """Завершить процессы по имени. Запасной путь для оконных нет.

    Вызывается, когда у процесса нет видимых окон: у служб и фоновых
    утилит их не бывает вовсе.
    """
    import psutil

    killed = 0
    target = (target or "").lower().replace(".exe", "").strip()
    if not target:
        return 0

    own_pid = psutil.Process().pid

    for process in psutil.process_iter(["pid", "name"]):
        try:
            info = process.info
            name = (info.get("name") or "").lower().replace(".exe", "")
            # Свой процесс и критичные службы не трогаем.
            if info.get("pid") == own_pid or not name:
                continue
            if _matches(name, target) and name not in _PROTECTED:
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
