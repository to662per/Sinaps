"""Тесты кодировки PowerShell и русских путей.

Оба бага выглядели как «ассистент сломался», а были разными:

* кодировка — `stdout` приходил как `None`, и навык падал вместо
  ответа. Причина в том, что `AttributeError` возникает при чтении
  потока, минуя `except Exception` на стороне вызова;
* пути — русские названия папок не разворачивались вовсе, и навык с
  примером «открой папку загрузки» не мог сработать в принципе.

Проверки на живом PowerShell: баг проявляется только там. Заглушка
subprocess его не покажет.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(subprocess.run(
    ["powershell", "-NoProfile", "-Command", "exit 0"],
    capture_output=True).returncode != 0,
    reason="нужен Windows с PowerShell")


def _run(script: str, timeout: float = 20.0) -> str:
    from sinaps.platform.windows import _run_powershell

    return _run_powershell(script, timeout=timeout)


# --- кодировка --------------------------------------------------------------


def test_cyrillic_survives_roundtrip():
    """Имена ярлыков кириллицей — ради них всё и затевалось."""
    out = _run("Write-Output 'Яндекс Браузер|chrome.exe'")
    assert "Яндекс Браузер" in out, f"кириллица потерялась: {out!r}"
    assert "chrome.exe" in out


def test_undecodable_byte_does_not_crash():
    """Байт, которого нет в cp1251, раньше ронял весь навык.

    Именно он приходил в именах ярлыков и ломал «покажи список
    программ» вместе с «открой файл».
    """
    out = _run("[char]0x98 + '|' + [char]0x99")
    # Раньше здесь был UnicodeDecodeError, а stdout был None.
    assert out is not None
    assert "|" in out, f"разделитель потерялся: {out!r}"


def test_empty_output_returns_empty_string():
    assert _run("") == ""


def test_failing_script_returns_empty_string():
    """Ошибка в PowerShell не должна подниматься в Python.

    `throw` возвращает ненулевой код, но не исключение: наверх уйдёт
    пустая строка, а список программ останется пустым, а не сломанным.
    """
    assert _run("throw 'сломано'") == ""


def test_helper_never_returns_none():
    """Главная гарантия: на выходе всегда строка.

    Раньше был `None`, и вызывающий код падал на `None.splitlines()`
    мимо своей обработки ошибок.
    """
    for script in ("", "throw 1", "Write-Output 'ок'", "exit 3"):
        assert isinstance(_run(script), str), f"{script!r} вернул не строку"


# --- в коде не осталось опасных вызовов ------------------------------------


def test_no_bare_text_true_for_powershell():
    """Страховка от возврата бага.

    `text=True` без `errors=` читает вывод в кодировке системы. Пока
    такие вызовы существуют, поломка может вернуться в новом месте.
    """
    source = Path("sinaps/platform/windows.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"):
            continue
        keywords = {kw.arg: kw for kw in node.keywords}
        if keywords.get("text") is None:
            continue        # байты: безопасно, мы декодируем сами
        if keywords.get("encoding") or keywords.get("errors"):
            continue        # кодировка задана явно: безопасно
        offenders.append(node.lineno)

    assert not offenders, (
        f"subprocess с text=True без encoding в строках {offenders}: "
        "вывод PowerShell будет расшифрован в кодировку системы"
    )


def test_powershell_helper_forces_utf8():
    """Кодировку надо задавать со стороны PowerShell, а не только Python.

    Иначе байты `0x98` дойдут уже испорченными и `errors='replace'`
    превратит кириллицу в вопросительные знаки.
    """
    source = Path("sinaps/platform/windows.py").read_text(encoding="utf-8")
    assert "OutputEncoding" in source, "PowerShell не переключён на UTF-8"
    assert 'errors="replace"' in source, "декодирование должно быть мягким"


# --- русские пути -----------------------------------------------------------


@pytest.mark.parametrize("name", [
    "загрузки", "Загрузки", "документы", "рабочий стол",
    "изображения", "картинки", "музыка", "видео",
])
def test_russian_folder_names_resolve(name):
    """Навык обещает «открой папку загрузки» — значит, оно должно работать."""
    from sinaps.platform.windows import _well_known_dir

    resolved = _well_known_dir(name)
    assert resolved, f"«{name}» не развернулось в путь"
    assert Path(resolved).exists()


@pytest.mark.parametrize("name", ["downloads", "documents", "pictures"])
def test_english_folder_names_still_work(name):
    """Правка не должна сломать английские названия."""
    from sinaps.platform.windows import _well_known_dir

    assert _well_known_dir(name)


def test_unknown_name_is_not_treated_as_folder():
    """Лишнее имя не должно превращаться в путь."""
    from sinaps.platform.windows import _well_known_dir

    assert _well_known_dir("какая-то папка") is None
    assert _well_known_dir("") is None


def test_open_path_accepts_russian_folder():
    """Проверка на настоящей платформе, а не на заглушке."""
    from sinaps.platform.detect import build_platform

    platform = build_platform()
    assert platform.open_path("загрузки"), "папка загрузки не открывается"


# --- фразы для списка программ ----------------------------------------------


@pytest.mark.parametrize("phrase", [
    "покажи список доступных программ",
    "какие программы доступны",
    "покажи список приложений",
    "какие приложения доступны",
    "сколько программ",
    "список программ",
    "что у меня установлено",
    "что ты умеешь открывать",
])
def test_program_list_phrases_are_understood(phrase):
    """Формулировки, которыми пользуются люди, а не автор команд.

    Список команд пишет «какие программы доступны», а человек
    говорит «покажи список доступных программ». Разница в одном слове,
    а ответ был «не понял».
    """
    import asyncio

    from sinaps.core.bus import EventBus
    from sinaps.core.session import SessionController
    from sinaps.skills.engine import SkillEngine, build_default_registry
    from sinaps.plugins.testing import FakePlatform

    engine = SkillEngine(registry=build_default_registry(),
                         bus=EventBus(),
                         session=SessionController(EventBus()),
                         platform=FakePlatform())

    async def ask():
        return await engine.respond(phrase, {"source": "voice"})

    reply = asyncio.run(ask())
    assert reply.meta.get("intent") == "apps.list", (
        f"{phrase!r} не распознана: {reply.speech!r}"
    )


@pytest.mark.parametrize("phrase", [
    "открой блокнот",
    "сделай громче",
    "который час",
    "открой папку загрузки",
])
def test_list_pattern_does_not_swallow_other_commands(phrase):
    """Новые шаблоны не должны перехватывать чужие команды."""
    import asyncio

    from sinaps.core.bus import EventBus
    from sinaps.core.session import SessionController
    from sinaps.skills.engine import SkillEngine, build_default_registry
    from sinaps.plugins.testing import FakePlatform

    engine = SkillEngine(registry=build_default_registry(),
                         bus=EventBus(),
                         session=SessionController(EventBus()),
                         platform=FakePlatform())

    async def ask():
        return await engine.respond(phrase, {"source": "voice"})

    reply = asyncio.run(ask())
    assert reply.meta.get("intent") != "apps.list", (
        f"{phrase!r} перехвачена шаблоном списка программ"
    )