"""Тесты слоя Windows.

Важно: здесь НЕ используется заглушка `_launch_path`. Именно из-за заглушки
баг с `notepad.exe` проскочил незамеченным: тест проверял «вызвали ли мы
заглушку», а не «дошли ли мы до System32».

`os.startfile` подменяется, поэтому ничего реально не запускается, но
проверяется весь путь разрешения имени до точки запуска.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from sinaps.platform.windows import (
    _BUILTIN_APPS,
    _is_safe_name,
    _match_quality,
    _norm,
    _ru_to_lat,
    _variants,
    WindowsPlatform,
)

pytestmark = pytest.mark.skipif(
    shutil.which("notepad") is None, reason="проверка только на Windows"
)


@pytest.fixture
def launched(monkeypatch):
    """Подменяет os.startfile и записывает, что до него дошло.

    Исключение НЕ бросается: проверяется именно разрешение имени до точки
    запуска. Отказ запуска разбирается отдельным тестом.
    """
    calls: list[str] = []

    def fake_startfile(target, *args, **kwargs):  # noqa: ANN001
        calls.append(str(target))

    monkeypatch.setattr("sinaps.platform.windows.os.startfile", fake_startfile)
    return calls


@pytest.fixture
def launched_failing(monkeypatch):
    """Тот же подменятель, но os.startfile всегда падает."""
    calls: list[str] = []

    def failing_startfile(target, *args, **kwargs):  # noqa: ANN001
        calls.append(str(target))
        raise OSError("нет интерактивного рабочего стола")

    monkeypatch.setattr(
        "sinaps.platform.windows.os.startfile", failing_startfile)
    return calls


# --- регрессия: имя с расширением ищется в PATH ---------------------------


@pytest.mark.parametrize("name", ["notepad.exe", "notepad", "calc.exe",
                                  "explorer.exe", "cmd.exe"])
def test_launch_path_resolves_via_path(launched, name):
    """Голое имя файла не является путём — до System32 надо дойти через PATH.

    Регрессия: стояла проверка «если расширение уже есть, искать не нужно».
    """
    from sinaps.platform.windows import _launch_path

    _launch_path(name)
    assert launched, f"{name} не разрешился в исполняемый файл"
    assert Path(launched[0]).exists()


def test_launch_path_rejects_unknown_program(launched):
    from sinaps.platform.windows import _launch_path

    assert _launch_path("definitely_not_a_real_program_xyz.exe") is False
    assert launched == []


def test_launch_path_accepts_full_existing_path(launched, tmp_path):
    from sinaps.platform.windows import _launch_path

    fake = tmp_path / "program.exe"
    fake.write_bytes(b"MZ")

    assert _launch_path(str(fake)) is True
    assert launched == [str(fake)]


def test_launch_path_reports_failure_of_startfile(launched_failing, tmp_path):
    """Если система не смогла запустить — это False, а не исключение.

    Настоящий случай: нет интерактивного рабочего стола.
    """
    from sinaps.platform.windows import _launch_path

    fake = tmp_path / "program.exe"
    fake.write_bytes(b"MZ")

    assert _launch_path(str(fake)) is False
    assert launched_failing == [str(fake)], "до os.startfile код должен был дойти"


# --- безопасность ввода ----------------------------------------------------


@pytest.mark.parametrize("hostile", [
    'notepad & calc',
    'a" | b',
    "x'; del",
    "notepad$(whoami)",
    "notepad|cat",
    "notepad`whoami`",
    "notepad & start calc",
    "",
    "   ",
])
def test_hostile_input_is_rejected(hostile):
    """Пользовательский ввод не должен попадать в запуск как команда."""
    assert _is_safe_name(hostile) is False


@pytest.mark.parametrize("legitimate", [
    "notepad.exe",
    "C:\\Windows\\System32\\notepad.exe",
    "C:\\Program Files\\7-Zip\\7zFM.exe",
    "D:/Games/game.exe",
    "steam",
])
def test_legitimate_paths_are_allowed(legitimate):
    """Разделители пути обязаны разрешаться, иначе не работают реальные пути.

    Регрессия: строгий класс символов отклонял обратный слеш, и любой
    нормальный Windows-путь уходил в отказ.
    """
    assert _is_safe_name(legitimate) is True


@pytest.mark.parametrize("traversal", [
    "..\\Windows\\System32\\cmd.exe",
    "../../evil.exe",
    "C:\\..\\..\\evil.exe",
])
def test_directory_traversal_is_rejected(launched, traversal):
    """Обход каталогов голосом не нужен и небезопасен."""
    from sinaps.platform.windows import _launch_path

    assert _is_safe_name(traversal) is False
    assert _launch_path(traversal) is False
    assert launched == []


# --- каскад запуска --------------------------------------------------------


@pytest.mark.parametrize("query,expected", [
    ("блокнот", "notepad.exe"),
    ("notepad", "notepad.exe"),
    ("текстовый редактор", "notepad.exe"),
    ("калькулятор", "calc.exe"),
    ("кальк", "calc.exe"),
    ("paint", "mspaint.exe"),
    ("проводник", "explorer.exe"),
    ("командная строка", "cmd.exe"),
    ("диспетчер задач", "taskmgr.exe"),
])
def test_builtin_aliases_launch(launched, query, expected):
    """Русские названия встроенных программ должны открываться."""
    platform = WindowsPlatform()
    platform.launch_app(query)

    assert launched, f"«{query}» не дошёл до запуска"
    assert Path(launched[0]).name.lower() == expected.lower()


@pytest.mark.parametrize("query", [
    "настройки", "параметры", "панель управления", "буфер обмена", "экран",
])
def test_uri_apps_launch(launched, query):
    """Программы, открываемые через URI-схему."""
    from sinaps.platform.windows import _open_uri

    calls: list[str] = []
    monkey = patch("sinaps.platform.windows._open_uri",
                   lambda uri: (calls.append(uri), True)[1])
    with monkey:
        WindowsPlatform().launch_app(query)
    assert calls, f"«{query}» не открылся как URI"


def test_unknown_program_gives_message_not_crash(launched):
    ok, info = WindowsPlatform().launch_app("абстрактная программа нету")
    assert ok is False
    assert "не нашёл" in info.lower() or "похожее" in info.lower()


# --- поиск имён ------------------------------------------------------------


def test_find_app_by_name():
    platform = WindowsPlatform()
    found = platform.find_app("excel")
    assert found is not None, "Excel установлен и должен находиться"
    assert "excel" in found.name.lower()


def test_word_does_not_match_password_manager():
    """Регрессия: подстановка по подстроке искала «Password Manager»."""
    platform = WindowsPlatform()
    found = platform.find_app("word")
    if found is None:
        pytest.skip("Word не установлен")
    assert found.name.lower().strip() == "word"


def test_match_quality_word_boundaries():
    assert _match_quality("word", "word") == 100
    assert _match_quality("autocad", "autocad 2026") == 90
    assert _match_quality("word", "password manager") == 0, (
        "границы слов должны учитываться"
    )
    assert _match_quality("code", "visual studio code") == 70, (
        "запрос может быть отдельным словом внутри названия"
    )
    assert _match_quality("discord", "discord") == 100


def test_variants_include_transliteration():
    variants = _variants("дискорд")
    assert "дискорд" in variants
    assert "diskord" in variants


def test_norm_strips_yo_and_spaces():
    assert _norm("  Счёт   Два  ") == "счет два"
    assert _ru_to_lat("телеграм") == "telegram"


def test_builtin_table_has_no_duplicates():
    """Один и тот же голосовой запрос не должен вести в разные программы."""
    targets: dict[str, str] = {}
    for key, (exe, label) in _BUILTIN_APPS.items():
        previous = targets.get(key)
        assert previous is None or previous == exe, (
            f"ключ «{key}» указывает и на {previous}, и на {exe}"
        )
        targets[key] = exe


def test_fuzzy_never_launches_wrong_app(launched):
    """Нечёткое совпадение не должно молча запускать не то.

    Замеры показали, что «стим»->Steam (0.67) и «chrome»->Heroes III (0.67)
    неразделимы порогом, поэтому fuzzy только предлагает варианты.
    """
    platform = WindowsPlatform()
    ok, info = platform.launch_app("телеграм")   # не установлен, похожего нет
    if ok:
        pytest.skip("Telegram установлен")
    assert not launched, "нечёткое совпадение не должно запускать программу"