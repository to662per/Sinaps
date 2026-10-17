# -*- coding: utf-8 -*-
"""Плагин получает звонок в срок и отвечает на него.

Ядро умеет будить плагина (`OP_FIRE`). Если раннер такой звонок не
разбирает, срок просто сгорает: человек поставил напоминание, а тишина
выглядит как «напоминания не работают».

Проверяется и вторая половина: `on_fire` приходит **без контекста** —
у него нет ни запроса, ни слотов. Заговорить он всё равно должен, а для
этого контекст надо сохранить при регистрации. Без этого `on_fire`
поднимет `AttributeError`, и человек услышит тишину вместо звонка.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def sent(monkeypatch):
    """Что плагин отвечает ядру."""
    out: list[dict] = []
    monkeypatch.setattr("sinaps.plugins.runner._write", out.append)
    return out


@pytest.fixture
def fired(monkeypatch):
    """Вызывался ли `on_fire`, и с каким payload."""
    seen: list[dict] = []

    class _Mod:
        @staticmethod
        async def on_fire(payload):
            seen.append(payload)

    monkeypatch.setattr("sinaps.plugins.runner._CONTEXT", object())
    return seen, _Mod()


def test_fire_reaches_the_plugin(fired, sent):
    """Звонок доходит до `on_fire` и получает полезную нагрузку."""
    from sinaps.plugins.runner import _handle_fire

    seen, module = fired
    _handle_fire({"plan_id": "timer-1", "payload": {"why": "Пора"}}, module)

    assert seen == [{"why": "Пора"}], "плагин не позвали"
    assert sent[-1]["op"] == "fire_ok"
    assert sent[-1]["plan_id"] == "timer-1"


def test_plugin_without_on_fire_is_told(fired, sent):
    """Нет обработчика — ядро узнаёт об отказе.

    Молчаливый успех был бы хуже: запись отметилась бы отработавшей,
    и человек не узнал бы, что его забыли.
    """
    from sinaps.plugins.runner import _handle_fire

    _seen, _module = fired

    class _Empty:
        pass

    _handle_fire({"plan_id": "timer-1", "payload": {}}, _Empty())

    assert sent[-1]["op"] == "fire_fail"
    assert "on_fire" in sent[-1]["message"]


def test_failing_plugin_does_not_escape(fired, sent):
    """Падение `on_fire` не роняет плагин и не выглядит как успех."""
    from sinaps.plugins.runner import _handle_fire

    class _Boom:
        @staticmethod
        async def on_fire(_payload):
            raise RuntimeError("плагин сломался")

    _handle_fire({"plan_id": "timer-1", "payload": {}}, _Boom())

    assert sent[-1]["op"] == "fire_fail"
    assert sent[-1]["type"] == "RuntimeError"


def test_missing_context_is_not_a_silent_success(fired, sent, monkeypatch):
    """Нет контекста — это отказ, а не успех.

    Так и было бы: контекст сохраняется при регистрации, и если его
    нет, плагин физически не сможет заговорить. Отметить успехом —
    значит человек не услышит ничего и не поймёт почему.
    """
    from sinaps.plugins import runner as runner_module

    monkeypatch.setattr(runner_module, "_CONTEXT", None)

    seen: list = []

    class _Mod:
        @staticmethod
        async def on_fire(payload):
            seen.append(payload)

    runner_module._handle_fire({"plan_id": "timer-1", "payload": {}}, _Mod())

    assert seen == [], "вызов состоялся без контекста"
    assert sent[-1]["op"] == "fire_fail"


def test_sync_handler_is_accepted(fired, sent):
    """Обработчик может быть и без `async`."""
    from sinaps.plugins.runner import _handle_fire

    seen: list = []

    class _Mod:
        @staticmethod
        def on_fire(payload):
            seen.append(payload)

    _handle_fire({"plan_id": "timer-1", "payload": {"a": 1}}, _Mod())

    assert seen == [{"a": 1}]
    assert sent[-1]["op"] == "fire_ok"


def test_context_is_created_before_register(monkeypatch):
    """Контекст кладётся в реестр до `register`.

    Иначе плагин не достанет его при регистрации, а `on_fire`
    останется без контекста — и таймер сработает молча.
    """
    import sinaps.plugins.runner as runner_module

    assert hasattr(runner_module, "_make_context")
    assert hasattr(runner_module, "_StateProxy")


def test_entry_module_is_kept(tmp_path):
    """Модуль плагина сохраняется при загрузке: `on_fire` лежит в модуле.

    `_load_entry` возвращает только функцию `register`, а звонок
    приходит уже после регистрации — искать обработчик больше негде.
    Проверяется вызовом, а не существованием списка.
    """
    from sinaps.plugins.runner import _ENTRY_MODULE, _load_entry

    plugin = tmp_path / "p.py"
    plugin.write_text(
        "async def on_fire(payload):\n"
        "    return payload\n",
        encoding="utf-8")

    before = len(_ENTRY_MODULE)
    entry = _load_entry(tmp_path, "p", "on_fire")

    assert callable(entry)
    assert len(_ENTRY_MODULE) == before + 1, (
        "модуль не сохранён — on_fire потом не найдётся")
    assert hasattr(_ENTRY_MODULE[-1], "on_fire")


def test_schedule_capability_without_permission_is_refused(tmp_path,
                                                       monkeypatch):
    """Без права `schedule` — отказ, а не срок, который не сработает."""
    import asyncio

    from sinaps.plugins.loader import PluginProcess
    from sinaps.plugins.manifest import Manifest
    from sinaps.plugins.protocol import CAP_SCHEDULE, OP_CAP

    manifest = Manifest(
        name="timer", version="1.0.0", description="Таймер",
        entry="plugin:register", permissions=(CAP_SCHEDULE,))
    proc = PluginProcess(tmp_path, manifest, granted=())

    class _Proc:               # нужен для проверки живости и stdin
        stdin = None
        returncode = None
    proc._proc = _Proc()
    sent: list[dict] = []

    async def _write(_stream, _msg):
        sent.append(_msg)

    # Подмена через monkeypatch: простая присваивание жила бы дальше
    # и следующему тесту досталась бы функция, пишущая в умерший список.
    monkeypatch.setattr("sinaps.plugins.protocol.write_message_async", _write)

    async def _go():
        await proc._serve_capability({
            "op": OP_CAP, "cap": CAP_SCHEDULE, "action": "at",
            "kwargs": {"plan_id": "timer-1", "delay_s": 300}})

    asyncio.run(_go())

    assert sent, "ядро не ответило ничего — плагин повиснет"
    assert sent[0]["op"] != "cap_ok", (
        "ответ об успехе без права schedule — плагин будет ждать звонка")
    assert sent[0].get("type") == "PermissionDenied"