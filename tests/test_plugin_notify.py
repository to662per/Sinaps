# -*- coding: utf-8 -*-
"""Плагин может показать уведомление и заговорить сам.

Право `notify` было объявлено в манифестах и в списке прав, но за ним
не стояло ни одной функции: плагин мог получить разрешение и не суметь
им воспользоваться. Таймер без этого бесполезен — сработав, он не
показал бы окно и не сказал бы вслух.

Проверяется всё три звена: право проверяется **до** показа, текст
доходит, без права плагин получает отказ, а не тишину.
"""

from __future__ import annotations

import pytest

from sinaps.plugins.protocol import CAP_NOTIFY, CAP_STATE


@pytest.fixture
def shown() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def plugin(shown, tmp_path):
    """Плагин с правом `notify`, готовый принять уведомление."""
    from sinaps.plugins.manifest import Manifest
    from sinaps.plugins.loader import PluginProcess

    manifest = Manifest(
        name="timer", version="1.0.0", description="Таймер",
        entry="plugin:register", permissions=("notify", CAP_STATE),
    )
    proc = PluginProcess(tmp_path, manifest,
                         granted=("notify", CAP_STATE))
    proc._notify_handler = lambda title, text: shown.append((title, text))
    return proc


def test_notification_reaches_the_app(plugin, shown):
    """Текст уведомления доходит до окна."""
    assert plugin._notify_action({"text": "Время пить чай"}) is True
    assert shown == [("Sinaps", "Время пить чай")]


def test_notification_carries_its_title(plugin, shown):
    """Заголовок не теряется: он и есть причина, почему сработало."""
    plugin._notify_action({"text": "Пять минут", "title": "Таймер"})
    assert shown[0][0] == "Таймер"


def test_empty_notification_is_refused(plugin, shown):
    """Пустое уведомление не показывается.

    Окно с пустым заголовком выглядит как сбой, а не как сообщение.
    """
    from sinaps.core.errors import PluginError

    with pytest.raises(PluginError):
        plugin._notify_action({"text": "   "})
    assert shown == []


def test_permission_is_checked_before_showing(plugin, shown):
    """Без права `notify` — отказ, и ничего не показано.

    Проверка после показа означала бы «мы не покажем результат», а не
    «не делай». Тишина в ответ на попытку выглядит как «ассистент
    сломался», и человек не понимает, что произошло.
    """
    from sinaps.core.errors import PermissionDenied

    plugin.granted = (CAP_STATE,)
    plugin._notify_handler = lambda t, x: shown.append((t, x))

    with pytest.raises(PermissionDenied):
        plugin._notify_action({"text": "Покажи меня"})
    assert shown == [], "уведомление показано без разрешения"


def test_missing_handler_fails_visibly(plugin, shown):
    """Показывать некому — плагин узнаёт об этом, а не гадает.

    Иначе таймер молчал бы, и человек решил бы, что он сломан.
    """
    plugin._notify_handler = None

    assert plugin._notify_action({"text": "Тишина"}) is False


def test_handler_failure_does_not_break_the_plugin(plugin, shown):
    """Сбой показа не поднимается наружу.

    Плагин не должен падать из-за окна: он начнёт следующий таймер и
    потеряет всю очередь. Но лог остаётся.
    """
    def _boom(title, text):
        raise RuntimeError("трея нет")

    plugin._notify_handler = _boom

    assert plugin._notify_action({"text": "Показ"}) is False


def test_state_still_works_alongside(plugin, shown):
    """Добавление `notify` не сломало `state`.

    Права проверяются по одному; неосторожная правка могла бы сделать
    `state` недоступным для плагина, который его запросил.
    """
    from sinaps.plugins.loader import _state_action

    _state_action(plugin._state, "set", {"key": "k", "value": 1})
    assert _state_action(plugin._state, "get", {"key": "k"}) == 1


def test_notify_is_a_known_capability():
    """`notify` объявлен в протоколе, а не придуман на месте."""
    from sinaps.plugins.manifest import KNOWN_PERMISSIONS

    assert CAP_NOTIFY == "notify"
    assert CAP_NOTIFY in KNOWN_PERMISSIONS


def test_state_action_get_set_delete(plugin, monkeypatch):
    """Плагин может читать, записывать и удалять состояние."""
    from sinaps.plugins.runner import _handle_cap, _plugin_state
    from sinaps.plugins.protocol import CAP_STATE, OP_CAP_OK
    from sinaps.plugins.state import PluginState

    # Подменяем глобальное состояние плагина на временное
    state = PluginState(plugin="test_state_action")
    monkeypatch.setattr("sinaps.plugins.runner._plugin_state", state)

    # get
    state.set("key1", "value1")
    state.set("key2", 42)
    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "get", "kwargs": {"key": "key1"}},
                        plugin.granted)
    assert reply == {"op": OP_CAP_OK, "value": "value1"}

    # get отсутствующего ключа
    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "get", "kwargs": {"key": "nope",
                                                     "default": None}},
                        plugin.granted)
    assert reply == {"op": OP_CAP_OK, "value": None}

    # set
    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "set", "kwargs": {"key": "key3",
                                                     "value": [1, 2]}},
                        plugin.granted)
    assert reply == {"op": OP_CAP_OK, "value": None}
    assert state.get("key3") == [1, 2]

    # delete
    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "delete", "kwargs": {"key": "key1"}},
                        plugin.granted)
    assert reply == {"op": OP_CAP_OK, "value": True}
    assert state.get("key1") is None

    # delete отсутствующего ключа
    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "delete", "kwargs": {"key": "nope"}},
                        plugin.granted)
    assert reply == {"op": OP_CAP_OK, "value": False}

    # keys
    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "keys", "kwargs": {}},
                        plugin.granted)
    assert reply["op"] == OP_CAP_OK
    assert set(reply["value"]) == {"key2", "key3"}

    # all
    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "all", "kwargs": {}},
                        plugin.granted)
    assert reply["value"] == {"key2": 42, "key3": [1, 2]}

    # clear
    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "clear", "kwargs": {}},
                        plugin.granted)
    assert reply == {"op": OP_CAP_OK, "value": 2}
    assert state.all() == {}


def test_state_action_unknown_capability(plugin):
    """Неизвестная возможность — отказ, а не молчание."""
    from sinaps.plugins.runner import _handle_cap
    from sinaps.plugins.protocol import OP_CAP_FAIL

    reply = _handle_cap({"op": "cap", "cap": "network.http",
                         "action": "get", "kwargs": {}},
                        plugin.granted)
    assert reply["op"] == OP_CAP_FAIL


def test_state_action_without_permission(plugin):
    """Без права `state` — отказ, а не тишина."""
    from sinaps.plugins.runner import _handle_cap
    from sinaps.plugins.protocol import CAP_STATE, OP_CAP_FAIL

    plugin.granted = ("notify",)
    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "get", "kwargs": {"key": "k"}},
                        plugin.granted)
    assert reply["op"] == OP_CAP_FAIL


def test_state_action_unknown_action(plugin):
    """Неизвестное действие — отказ."""
    from sinaps.plugins.runner import _handle_cap
    from sinaps.plugins.protocol import CAP_STATE, OP_CAP_FAIL

    reply = _handle_cap({"op": "cap", "cap": CAP_STATE,
                         "action": "fly", "kwargs": {}},
                        plugin.granted)
    assert reply["op"] == OP_CAP_FAIL