"""Тесты запуска программ.

Жалоба: «говорит что открывает, но ничего не меняется». Проверка дала
две наложившиеся ошибки, и обе выглядели как «ассистент врёт»:

1. `_SAFE_NAME` не разрешал скобки, а половина программ стоит в
   «Program Files (x86)». Notepad запускался, Edge — нет.
2. `launch_app` на найденной программе возвращал успех, **не вызывая
   запуск**. Навык говорил «Открываю Microsoft Edge» — и ничего не
   происходило.

Вторая ошибка маскировала первую: раз путь отбрасывался проверкой,
она и выглядела главной.
"""

from __future__ import annotations

import pytest

from sinaps.platform import windows as W


# --- проверка имени ---------------------------------------------------------


@pytest.mark.parametrize("path", [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Windows\system32\notepad.exe",
    r"D:\Игры\Steam\steam.exe",
    r"C:\Users\Артём\Downloads\файл (1).txt",
])
def test_parentheses_allowed(path):
    """Скобки в пути — не угроза, а норма для Windows.

    Запрет был безобидным на вид, но из-за него не запускался ни один
    32-битный браузер или редактор: они все стоят в «Program Files (x86)».
    """
    assert W._is_safe_name(path), f"{path} отклонён как небезопасный"


@pytest.mark.parametrize("bad", [
    "calc & del",
    "calc | more",
    "calc > out.txt",
    "calc ; calc",
    "C:\\..\\Windows\\System32\\cmd.exe",
    "..\\..\\секрет.txt",
    "",
    "   ",
])
def test_dangerous_names_still_rejected(bad):
    """Защиту нельзя ослаблять вместе с исправлением.

    `&`, `|`, `>` и выход через `..` — ровно то, от чего она стояла.
    """
    assert not W._is_safe_name(bad), f"{bad!r} не должен проходить"


def test_traversal_still_rejected_with_parens():
    """Скобки разрешены, обход каталогов — по-прежнему нет."""
    assert not W._is_safe_name(r"C:\Program Files (x86)\..\..\Windows\cmd.exe")


# --- запуск найденной программы ---------------------------------------------


class _FakePlatform(W.WindowsPlatform):
    """Платформа с заранее известным списком программ."""

    def __init__(self, target: str, launches: list[str]) -> None:      # noqa: ANN001
        self._target = target
        self._launches = launches

    def list_apps(self):                                           # noqa: ANN001
        from sinaps.platform.base import AppInfo

        return [AppInfo(name="Microsoft Edge", target=self._target)]


def _record_launches(monkeypatch, *, succeed_for: str = "") -> list[str]:
    """Заглушка запуска, похожая на настоящую.

    Важно: `_launch_path` возвращает True только для пути к файлу.
    Настоящая так и делает — `имя_команды` до System32 доходит через
    `shutil.which`. Заглушка, которая всегда отвечает «да», перехватывает
    всё на третьем шаге и проверка становится бессмысленной.
    """
    launched: list[str] = []

    def fake(name: str) -> bool:                    # noqa: ANN001
        launched.append(name)
        return bool(succeed_for) and succeed_for.lower() in name.lower()

    monkeypatch.setattr(W, "_launch_path", fake)
    return launched


def test_found_app_is_actually_launched(monkeypatch):
    """Главная проверка: найденная программа должна запускаться.

    Раньше здесь было `return True, found.target` — ответ «да» без
    единого действия. Навык рапортовал «Открываю», и человек смотрел
    на пустой экран.
    """
    launched = _record_launches(monkeypatch, succeed_for="msedge")

    platform = _FakePlatform(r"C:\Program Files (x86)\Edge\msedge.exe", launched)
    ok, info = platform.launch_app("microsoft edge")

    assert ok is True
    assert any("msedge" in name for name in launched), (
        f"программа найдена, но запуск не вызван: {launched}"
    )
    assert "Edge" in info


def test_failed_launch_is_reported_honestly(monkeypatch):
    """Если запуск не удался — говорим об этом, а не «открываю»."""
    _record_launches(monkeypatch)          # ни одна попытка не удаётся

    platform = _FakePlatform(r"C:\Program Files (x86)\Edge\msedge.exe", [])
    ok, info = platform.launch_app("microsoft edge")

    assert ok is False, "нельзя рапортовать об успехе, если ничего не запустилось"
    assert "Не удалось" in info


def test_unknown_app_is_not_launched(monkeypatch):
    """Мусор не должен ничего запускать.

    Попытка запустить имя «как есть» — штатный шаг, её делать нужно.
    Проверяем, что ничего не запустилось, а не что попытки не было.
    """
    launched = _record_launches(monkeypatch)
    monkeypatch.setattr(W, "_BUILTIN_APPS", {})
    monkeypatch.setattr(W.WindowsPlatform, "list_apps", lambda self: [])

    platform = W.WindowsPlatform()
    ok, _info = platform.launch_app("ывафыва")

    assert ok is False, "мусор не должен считаться удачным запуском"
    assert not any("\\" in name or "/" in name for name in launched), (
        f"ничего не должно запускаться по пути: {launched}"
    )


def test_builtin_app_takes_priority(monkeypatch):
    """«Блокнот» идёт через встроенный список, а не через поиск."""
    launched = _record_launches(monkeypatch, succeed_for="notepad")

    platform = _FakePlatform(r"C:\some\edge.exe", launched)
    ok, _info = platform.launch_app("блокнот")

    assert ok is True
    assert any("notepad" in name for name in launched)


# --- пути, где программы стоят по умолчанию --------------------------------


def test_program_files_x86_is_launchable(monkeypatch):
    """Проверка на настоящем пути, где стоит Edge."""
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

    assert W._is_safe_name(edge), (
        "путь с (x86) должен проходить проверку — иначе не запустится "
        "ни один 32-битный браузер"
    )
    assert W._launch_path(edge) is True


def test_notepad_launches(monkeypatch):
    monkeypatch.setattr(W.os, "startfile", lambda p: True)
    assert W._launch_path("notepad.exe") is True


def test_startfile_failure_is_not_fatal(monkeypatch):
    """Ошибка запуска не должна подниматься наружу."""
    def boom(_p):
        raise OSError(1223, "отменено пользователем")

    monkeypatch.setattr(W.os, "startfile", boom)
    assert W._launch_path("notepad.exe") is False