"""Витрина публичного API.

Повод
-----
Пока файла `sinaps/plugins/api.py` не было, плагин писал
`from sinaps.skills.base import Skill` — то есть зависел от внутренностей.
Стоит перенести класс или переименовать модуль, и все плагины ломались.
Объявлять совместимость приходилось со всем подряд, а это как раз то,
чего проект избегает.

Витрина разделеливает «всегда работает» (ядро) и «добавляют снаружи»
(плагины): плагину знать устройство ядра не положено.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sinaps.plugins import api

PACKAGE = Path(__file__).resolve().parent.parent / "sinaps"
PLUGINS = PACKAGE / "plugins"


# --- состав контракта -------------------------------------------------------


def test_contract_is_not_empty():
    assert api.contract(), "витрина пуста: плагину нечего импортировать"


@pytest.mark.parametrize("name", api.contract())
def test_every_exported_name_exists(name):
    """Имя в `__all__` обязано быть. Опечатка даст ImportError у автора."""
    assert hasattr(api, name), f"в __all__ есть {name!r}, но его нет в модуле"


def test_all_matches_contract():
    assert set(api.__all__) == set(api.contract()), (
        "__all__ и contract() разошлись"
    )


def test_no_duplicates_in_contract():
    assert len(api.contract()) == len(set(api.contract())), (
        "имя повторяется в контракте: автор плагина запутается, "
        "что именно он импортирует"
    )


def test_contract_covers_the_essentials():
    """Набор, без которого плагин не собрать.

    Проверяется явно, потому что витрина легко «раздувается»: сегодня
    добавили удобную функцию, завтра ещё одну, и через полгода обещание
    «не менять» перестаёт чего-то стоить.
    """
    essential = {
        "Skill", "IntentSpec", "SlotSpec", "Response", "SkillRegistry",
        "DialogContext", "SinapsError", "PluginError", "NetworkDisabled",
    }
    assert essential <= set(api.contract()), (
        f"в контракте не хватает: {sorted(essential - set(api.contract()))}"
    )


def test_internal_modules_are_not_exported():
    """Конвейер и хранилище — устройство, а не контракт.

    Экспорт `runtime` или `MemoryStore` означал бы, что автор плагина
    получит путь во внутренности, и мы обязаны держать их стабильными
    наравне с витриной.
    """
    forbidden = {"runtime", "engine", "bus", "MemoryStore",
                 "PlatformCapabilities", "ConversationPipeline"}
    leaked = forbidden & set(api.contract())
    assert not leaked, f"в контракт просочились внутренности: {sorted(leaked)}"


def test_speech_layers_are_not_exported():
    """Звук наружу не отдаётся — это основа приватности проекта."""
    for name in ("SileroTTS", "FasterWhisper", "SileroVAD", "AudioCapture"):
        assert name not in api.contract(), (
            f"{name} в контракте: плагин сможет отправить звук куда угодно"
        )


# --- витрина не врёт -------------------------------------------------------


def test_every_exported_name_is_importable_from_module():
    """Проверка через настоящий импорт, а не через hasattr.

    `hasattr` не ловит случай, когда имя есть, но не соответствует
    тому, что экспортируется: автор плагина пишет `from ... import X`
    и получает ImportError в первый же день.
    """
    for name in api.contract():
        module = __import__("sinaps.plugins.api", fromlist=[name])
        assert getattr(module, name) is getattr(api, name)


def test_api_module_has_no_side_effects_on_import():
    """Импорт витрины не должен ничего печатать и ничего запускать.

    Автор плагина будет импортировать её в каждом тесте; побочный
    эффект при импорте всплывёт как «тест падает из-за чужого плагина»
    и будет выглядеть как ошибка ядра.
    """
    tree = ast.parse((PLUGINS / "api.py").read_text(encoding="utf-8"))

    forbidden_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if isinstance(func, ast.Name) and func.id == "print":
            forbidden_calls.append("print")

    assert not forbidden_calls, (
        "в витрине есть вызовы при импорте: они сработают у каждого, "
        "кто её импортирует"
    )


# --- границы слоя -----------------------------------------------------------


def test_plugins_package_exists_with_only_its_own_files():
    """Слой плагинов не должен расползаться по ядру.

    Сейчас в `plugins/` только витрина. Загрузчик, манифест и разрешения
    появятся на следующих этапах — по одной вещи за раз, иначе непонятно,
    что именно сломалось.
    """
    files = sorted(p.name for p in PLUGINS.glob("*.py"))
    assert files == ["__init__.py", "api.py"], (
        f"в plugins/ есть лишнее: {files}"
    )


def test_api_does_not_import_network():
    """Витрина не должна тянуть сеть даже впрокладку.

    Ядро без сети — запрет на весь пакет `sinaps`, и витрина часть
    этого пакета. Сеть появится позже и в другом месте: вне пакета,
    как установщик.
    """
    tree = ast.parse((PLUGINS / "api.py").read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]

        for name in names:
            root = name.lstrip(".").split(".")[0]
            assert root not in ("httpx", "requests", "socket", "aiohttp"), (
                f"витрина тянет {name}: сеть в ядре запрещена"
            )
