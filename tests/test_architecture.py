"""Архитектурные тесты: правило зависимостей между слоями.

Смысл этих тестов не в коде, а в защите от будущего разъезда. Модульность
ломается не сразу, а постепенно: сначала «просто один импорт», потом ещё
десять, и через месяц слои связаны так, что навык уже нельзя вынести.

Правила, которые мы защищаем:
  1. `skills`, `ui`, `conversation`, `stt`, `tts` не знают друг о друге.
  2. Навык не может импортировать конкретную ОС — только `platform`.
  3. Никто, кроме `runtime.py` и `__main__.py`, не собирает граф вручную.
  4. `core` не зависит ни от одного верхнего слоя.
  5. Сетевые библиотеки не используются: проект строго локальный.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "sinaps"

#: Слои верхнего уровня, которые не должны зависеть друг от друга.
PEER_LAYERS = {
    "skills",
    "ui",
    "conversation",
    "stt",
    "tts",
    "vad",
    "audio",
    "memory",
    "integrations",
    "platform",
}

#: Кому можно импортировать себя из верхнего слоя.
#:
#: `skills` зависит от `platform` намеренно: навык «выключи компьютер»
#: обязан знать про PowerAction. Но знать про `winreg` или `pywin32` он
#: не должен — это проверяет отдельный тест `test_skills_do_not_import_os`.
ALLOWED_UPSTREAM = {
    "audio": {"core"},
    "vad": {"core", "audio"},
    "stt": {"core", "audio"},
    "tts": {"core", "audio"},
    "conversation": {"core", "audio", "stt", "tts", "vad"},
    "ui": {"core", "conversation"},
    "skills": {"core", "platform", "nlu"},
    "platform": {"core"},
    "memory": {"core"},
    "integrations": {"core"},
    "nlu": {"core"},
}

#: Модули, специфичные для ОС. Навык имеет право знать про `platform.base`
#: (это абстракция), но не про `platform.windows` (это реализация).
OS_SPECIFIC = {"platform.windows", "platform.macos", "platform.linux"}

#: Библиотеки, которые запрещены: проект не ходит в сеть.
FORBIDDEN_NETWORK = {
    "requests", "httpx", "aiohttp", "urllib3", "urllib.request",
    "socket", "http.client", "ftplib", "telnetlib",
}

#: Кто имеет право собирать граф зависимостей.
WIRING_ALLOWLIST = {"runtime.py", "__main__.py"}


def _module_name(path: Path) -> str:
    """'sinaps/core/bus.py' -> 'sinaps.core.bus'."""
    return ".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts)


def _python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _top_layer(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "sinaps" else None


def _imported_modules(tree: ast.AST) -> list[str]:
    """Все имена импортов в дереве (абсолютные и относительные)."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append("." * node.level + (node.module or ""))
            elif node.module:
                found.append(node.module)
    return found


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_file_parses(path: Path):
    """Синтаксическая проверка — ловит опечатки до запуска."""
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_network_imports(path: Path):
    """Требование проекта: строго локальная работа, без сети."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _imported_modules(tree):
        root = name.lstrip(".").split(".")[0]
        assert root not in FORBIDDEN_NETWORK, (
            f"{path.name}: запрещён сетевой импорт '{name}'. "
            "Проект работает локально; для сетевого режима нужен явный флаг."
        )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_layers_do_not_import_peers(path: Path):
    """Верхние слои не должны знать друг о друге напрямую."""
    layer = _top_layer(_module_name(path))
    if layer not in PEER_LAYERS:
        return

    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = ALLOWED_UPSTREAM.get(layer, set())

    for name in _imported_modules(tree):
        if name.startswith("."):
            # Относительный импорт внутри своего слоя — это законно.
            continue
        parts = name.split(".")
        if parts[0] != "sinaps" or len(parts) < 2:
            continue
        imported_layer = parts[1]
        if imported_layer not in PEER_LAYERS:
            continue
        assert imported_layer in allowed, (
            f"{path.name}: слой '{layer}' импортирует '{imported_layer}', "
            f"а разрешено только {sorted(allowed) or 'ничего из слоёв'}"
        )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_core_is_standalone(path: Path):
    """`core` — фундамент: он не зависит ни от одного верхнего слоя."""
    module = _module_name(path)
    if not module.startswith("sinaps.core"):
        return

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _imported_modules(tree):
        if name.startswith("."):
            continue
        parts = name.split(".")
        if parts[0] == "sinaps" and len(parts) > 1:
            assert parts[1] == "core", f"{path.name}: core не должен импортировать {name}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_wiring_lives_in_one_place(path: Path):
    """Собирать граф вручную можно только в runtime.py и __main__.py."""
    text = path.read_text(encoding="utf-8")
    if path.name not in WIRING_ALLOWLIST:
        assert "build_runtime(" not in text, (
            f"{path.name}: вызов build_runtime() разрешён только в "
            f"{sorted(WIRING_ALLOWLIST)}"
        )


def test_package_has_expected_layers():
    """Структура пакета не должна незаметно измениться."""
    present = {p.name for p in PACKAGE.iterdir() if p.is_dir() and p.name != "__pycache__"}
    expected = {"core", "audio", "stt", "tts", "vad", "conversation", "ui",
                "skills", "nlu", "platform"}
    assert expected <= present, f"Нет обязательных слоёв: {expected - present}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_skills_do_not_import_os(path: Path):
    """Навык не должен знать про конкретную ОС.

    Иначе «открыть приложение» придётся переписывать для каждой системы,
    и через месяц навык будет напрямую вызывать `winreg`.
    """
    module = _module_name(path)
    if not module.startswith("sinaps.skills"):
        return

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _imported_modules(tree):
        if name.startswith("."):
            name = module.rsplit(".", 1)[0] + name
            while "/." in name.replace("\\", "/"):
                name = name.replace("/..", "")
        if name in OS_SPECIFIC:
            raise AssertionError(
                f"{path.name}: навык импортирует реализацию ОС '{name}'. "
                "Разрешено только 'sinaps.platform.base' — это абстракция."
            )


def test_platform_abstraction_has_no_os_dependency():
    """`platform.base` — чистая абстракция: без ctypes, winreg, subprocess."""
    base = PACKAGE / "platform" / "base.py"
    tree = ast.parse(base.read_text(encoding="utf-8"))
    for name in _imported_modules(tree):
        root = name.lstrip(".").split(".")[0]
        assert root not in {"ctypes", "winreg", "subprocess", "os"}, (
            f"platform/base.py не должен импортировать {name}: "
            "это абстракция, детали ОС живут в platform/windows.py"
        )


def test_private_is_local_only_by_default():
    """Требование пользователя: по умолчанию никаких сетевых вызовов."""
    from sinaps.core.config import SinapsConfig

    assert SinapsConfig().privacy.local_only is True
