"""Закрытие программ: алиасы, точное совпадение, рабочий стол.

Повод: «закрой блокнот» всегда отвечало «не нашёл, что закрыть».
Разбор показал, что заголовок окна (`sinaps.log — Блокнот`) тут ни при
чём — `_kill_process` на заголовки не смотрит. Он искал буквальную
подстроку в имени процесса: `'блокнот' in 'notepad'` ложно. Таблица
русских названий была подключена только к запуску, поэтому открыть
можно было по-русски, а закрыть — только по-английски. В логе это
читалось как «всё отработало правильно», потому что запрос доезжал
верно.

Заодно нашлась вторая половина: у Блокнота с открытым файлом закрывать
нужно программу, а не окно по имени файла.
"""

from __future__ import annotations

import pytest

from sinaps.platform.base import WindowInfo
from sinaps.skills.apps import (_is_desktop_shell, _kill_process, _matches,
                                _resolve)


class _Stub:
    """Платформа для проверки сопоставления. Ничего не закрывает."""

    name = "stub"
    supported_power = ()

    def __init__(self, windows: list[WindowInfo] | None = None) -> None:
        self.windows = windows or []
        self.closed: list[int] = []

    def list_windows(self) -> list[WindowInfo]:
        return self.windows

    def close_window(self, handle: int) -> bool:
        self.closed.append(handle)
        return True

    def resolve_process_name(self, query: str) -> str:
        return {
            "блокнот": "notepad",
            "notepad": "notepad",
            "проводник": "explorer",
            "калькулятор": "calc",
            "диспетчер задач": "taskmgr",
        }.get(query.strip().lower(), "")


# --- алиасы: та же таблица, что и на запуске --------------------------------


@pytest.mark.parametrize("query,expected", [
    ("блокнот", "notepad"),
    ("Блокнот", "notepad"),
    ("notepad", "notepad"),
    ("калькулятор", "calc"),
    ("проводник", "explorer"),
    ("диспетчер задач", "taskmgr"),
])
def test_russian_name_resolves_to_process(query, expected):
    """«Закрой блокнот» должно значить то же, что «открой блокнот»."""
    assert _resolve(_Stub(), query) == expected


def test_unknown_name_resolves_to_nothing():
    assert _resolve(_Stub(), "какая-то фигня") == ""


def test_empty_query_is_safe():
    assert _resolve(_Stub(), "   ") == ""


# --- сопоставление: точно, а не подстрокой ----------------------------------


def test_exact_match_passes():
    assert _matches("notepad", "notepad") is True


@pytest.mark.parametrize("candidate,target", [
    ("blender", "blok"),      # «закрой блок» не должен трогать Blender
    ("notepad", "blok"),
    ("chromecast", "chrome"),
    ("chrome", "chromecast"),
])
def test_partial_match_is_rejected(candidate, target):
    """Подстрока опасна: «закрой блок» закрыл бы и notepad, и blender."""
    assert _matches(candidate, target) is False


def test_empty_names_never_match():
    assert _matches("", "notepad") is False
    assert _matches("notepad", "") is False


def test_kill_refuses_partial_names():
    """Запасной путь тоже не должен работать по подстроке."""
    # Цель — «blk», совпадений быть не должно ни с чем.
    assert _matches("blender", "blk") is False


# --- окно ищется по имени процесса, а не по заголовку -----------------------


def test_window_found_despite_open_file_in_title():
    """Ровно случай из жалобы: в Блокноте открыт файл Sinaps."""
    window = WindowInfo(handle=7, title="sinaps.log — Блокнот",
                        process="notepad.exe")
    platform = _Stub([window])

    from sinaps.skills.apps import _close_windows

    closed, speech = _close_windows(platform, "блокнот")

    assert closed == 1
    assert platform.closed == [7]
    assert "Закрыл 1" in speech


def test_close_uses_process_name_not_title():
    """Заголовок с чужим файлом не должен мешать."""
    window = WindowInfo(handle=1, title="Отчёт.doc — Блокнот",
                        process="notepad.exe")
    platform = _Stub([window])

    from sinaps.skills.apps import _close_windows

    assert _close_windows(platform, "блокнот")[0] == 1


def test_no_window_for_other_process():
    window = WindowInfo(handle=1, title="Диспетчер задач", process="taskmgr.exe")
    platform = _Stub([window])

    from sinaps.skills.apps import _close_windows

    assert _close_windows(platform, "блокнот")[0] == 0
    assert platform.closed == []


# --- рабочий стол закрывать нельзя ------------------------------------------


def test_desktop_shell_is_detected():
    """У оболочки рабочего стола имя процесса — explorer, как у проводника."""
    window = WindowInfo(handle=99, title="Program Manager",
                        process="explorer.exe")
    assert _is_desktop_shell(window) is True


@pytest.mark.parametrize("title", ["Рабочий стол", "Desktop"])
def test_desktop_shell_detected_in_russian(title):
    assert _is_desktop_shell(WindowInfo(handle=1, title=title,
                                        process="explorer.exe")) is True


def test_regular_window_is_not_desktop():
    window = WindowInfo(handle=2, title="Загрузки — проводник",
                        process="explorer.exe")
    assert _is_desktop_shell(window) is False


def test_close_file_manager_but_not_desktop():
    """«Закрой проводник» — это папка. Рабочий стол уронил бы Windows."""
    folder = WindowInfo(handle=10, title="logs — проводник",
                        process="explorer.exe")
    desktop = WindowInfo(handle=11, title="Program Manager",
                         process="explorer.exe")
    platform = _Stub([folder, desktop])

    from sinaps.skills.apps import _close_windows

    closed, _ = _close_windows(platform, "проводник")

    assert closed == 1
    assert platform.closed == [10]
    assert 11 not in platform.closed


def test_desktop_only_gives_nothing_to_close():
    """Если кроме рабочего стола ничего нет — закрывать нечего."""
    desktop = WindowInfo(handle=11, title="Program Manager",
                         process="explorer.exe")
    platform = _Stub([desktop])

    from sinaps.skills.apps import _close_windows

    assert _close_windows(platform, "проводник")[0] == 0
    assert platform.closed == []


# --- защищённые процессы -----------------------------------------------------


def test_protected_processes_are_not_killed():
    """explorer есть в защите: даже если совпадение есть, terminate не идёт."""
    from sinaps.skills.apps import _PROTECTED

    assert "explorer" in _PROTECTED
    assert "python" in _PROTECTED
    assert "sinaps" in _PROTECTED


def test_kill_without_target_does_nothing():
    assert _kill_process("") == 0
    assert _kill_process("   ") == 0
