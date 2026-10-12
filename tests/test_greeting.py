"""Тесты приветствия при старте.

Приветствие — первый звук, который слышит человек после включения
компьютера. Здесь важно не «произносится ли», а каким путём: отдельный
вызов синтеза создал бы второй путь к динамикам, минуя конвейер, и
позже разошёлся бы с ним по прерыванию и паузе.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture
def cfg():
    from sinaps.core.config import load_config

    return load_config()


# --- настройка --------------------------------------------------------------


def test_greet_on_start_defaults_to_true(cfg):
    """Человек ставит ассистента в автозагрузку и ждёт ответа при старте."""
    assert cfg.tts.greet_on_start is True


def test_greeting_can_be_switched_off(cfg):
    """Приветствие при каждой перезагрузке утомительно — выключается."""
    assert isinstance(cfg.tts.greet_on_start, bool)


# --- путь произнесения ------------------------------------------------------


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_tray_greeting_goes_through_conversation():
    """Трей должен здороваться через конвейер разговора.

    Отдельный вызов `tts.synthesize` в обход конвейера — это второй путь
    к динамикам: свой прерывание, своя пауза, своя очередь. Рано или
    поздно они разойдутся.
    """
    tree = ast.parse(_source("sinaps/ui/qt_host.py"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "handle_text_input"
    ]
    assert calls, "трей не здоровается при старте"


def test_console_greeting_goes_through_conversation():
    """Консольный режим должен вести себя так же.

    Иначе одно и то же поведение зависит от способа запуска, и
    непонятно, что считать правильным.
    """
    tree = ast.parse(_source("sinaps/__main__.py"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "handle_text_input"
    ]
    assert calls, "консольный режим не здоровается при старте"


def test_greeting_is_delayed_not_immediate():
    """Приветствие откладывается: пока грузятся модели, говорить нечем."""
    for path in ("sinaps/ui/qt_host.py", "sinaps/__main__.py"):
        text = _source(path)
        assert "call_later" in text, (
            f"{path}: приветствие ставится в очередь сразу и опоздает"
        )


def test_greeting_respects_the_setting():
    """Выключенное приветствие не должно произноситься."""
    for path in ("sinaps/ui/qt_host.py", "sinaps/__main__.py"):
        text = _source(path)
        assert "greet_on_start" in text, f"{path}: настройка не проверяется"


def test_no_direct_synthesize_bypass():
    """Нигде нельзя дергать синтез напрямую в обход конвейера."""
    for path in ("sinaps/ui/qt_host.py", "sinaps/ui/panel.py"):
        text = _source(path)
        assert "synthesize" not in text, (
            f"{path}: синтез вызывается мимо конвейера разговора"
        )


# --- что именно говорится ---------------------------------------------------


def test_greeting_text_is_synthesizable():
    """Текст приветствия должен реально озвучиваться, а не только храниться."""
    import asyncio

    from sinaps.core.bus import EventBus
    from sinaps.core.session import SessionController
    from sinaps.skills.engine import SkillEngine, build_default_registry
    from tests.conftest import FakePlatform

    engine = SkillEngine(registry=build_default_registry(),
                         bus=EventBus(),
                         session=SessionController(EventBus()),
                         platform=FakePlatform())

    async def ask():
        return await engine.respond("привет", {"source": "text"})

    reply = asyncio.run(ask())
    assert reply.speech.strip(), "приветствие должно что-то говорить"
    assert "Синапс" in reply.speech, "ассистент должен представиться"


def test_greeting_does_not_need_microphone():
    """Приветствие не должно требовать микрофона.

    Иначе на машине без входа поздравление не прозвучит — а это ровно
    тот случай, когда человек ждёт сигнала, что всё запустилось.
    """
    import asyncio

    from sinaps.core.bus import EventBus
    from sinaps.core.session import SessionController
    from sinaps.skills.engine import SkillEngine, build_default_registry
    from tests.conftest import FakePlatform

    engine = SkillEngine(registry=build_default_registry(),
                         bus=EventBus(),
                         session=SessionController(EventBus()),
                         platform=FakePlatform())

    async def ask():
        return await engine.respond("привет", {"source": "text"})

    reply = asyncio.run(ask())
    assert not reply.show_panel, "приветствие не должно открывать панель"
    assert reply.meta.get("intent") == "smalltalk.greeting", (
        "приветствие должно идти обычным навыком, а не отдельным кодом"
    )