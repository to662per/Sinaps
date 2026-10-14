"""Состояние плагина: ключи и значения, разделённые по плагинам.

Тесты быстрые и идут без процессов: `PluginState` — обычное
хранилище, а границу процесса проверяют тесты загрузчика.

Главное, что здесь защищается, — **разделение**. Плагин не должен видеть
данные другого плагина, даже если лежит с ним в одной папке и в одной
базе. Один запрос по чужому пространству имён — и приватность плагинов
перестаёт существовать.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sinaps.plugins.state import (MAX_KEY_LENGTH, PluginState, StateError,
                                  _check_key, _check_value)


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "plugins.db")


@pytest.fixture
def memo(db) -> PluginState:
    return PluginState(db, plugin="memo")


# --- обычная работа ---------------------------------------------------------


def test_missing_key_returns_default(memo):
    assert memo.get("нет-такого") is None
    assert memo.get("нет-такого", "запасное") == "запасное"


def test_set_then_get(memo):
    memo.set("город", "Москва")

    assert memo.get("город") == "Москва"


def test_overwrite(memo):
    memo.set("счётчик", 1)
    memo.set("счётчик", 2)

    assert memo.get("счётчик") == 2


def test_delete(memo):
    memo.set("город", "Москва")

    assert memo.delete("город") is True
    assert memo.get("город") is None


def test_delete_missing_key_is_not_an_error(memo):
    assert memo.delete("нет-такого") is False


def test_keys_and_all(memo):
    memo.set("а", 1)
    memo.set("б", "два")

    assert memo.keys() == ["а", "б"]
    assert memo.all() == {"а": 1, "б": "два"}


def test_clear_removes_everything(memo):
    memo.set("а", 1)
    memo.set("б", 2)

    assert memo.clear() == 2
    assert memo.keys() == []


def test_clear_on_empty_is_zero(memo):
    assert memo.clear() == 0


@pytest.mark.parametrize("value", [
    "строка", 42, 3.5, True, False, None,
    [1, 2, 3], {"вложенный": {"глубже": [1]}},
])
def test_json_values_round_trip(memo, value):
    memo.set("значение", value)

    assert memo.get("значение") == value


def test_russian_keys_and_values_survive(memo):
    """Кодировка не должна портить ни ключ, ни значение."""
    memo.set("город", "Нижний Новгород")

    assert memo.get("город") == "Нижний Новгород"


# --- разделение -------------------------------------------------------------


def test_plugins_do_not_see_each_other(db):
    first = PluginState(db, plugin="погода")
    second = PluginState(db, plugin="таймер")

    first.set("город", "Москва")

    assert second.get("город") is None
    assert second.keys() == []


def test_same_key_in_two_plugins_is_two_rows(db):
    first = PluginState(db, plugin="погода")
    second = PluginState(db, plugin="таймер")

    first.set("город", "Москва")
    second.set("город", "Казань")

    assert first.get("город") == "Москва"
    assert second.get("город") == "Казань"


def test_clear_of_one_plugin_leaves_another(db):
    first = PluginState(db, plugin="погода")
    second = PluginState(db, plugin="таймер")
    first.set("ключ", 1)
    second.set("ключ", 2)

    first.clear()

    assert second.get("ключ") == 2


def test_state_survives_new_object(db):
    """Новый `PluginState` — это новый процесс плагина, а не новый плагин."""
    PluginState(db, plugin="memo").set("город", "Москва")

    assert PluginState(db, plugin="memo").get("город") == "Москва"


def test_unknown_plugin_sees_nothing(db):
    PluginState(db, plugin="memo").set("город", "Москва")

    assert PluginState(db, plugin="чужой").keys() == []


# --- что хранить нельзя -----------------------------------------------------


@pytest.mark.parametrize("value", [
    object(),
    {1, 2, 3},
    lambda: None,
])
def test_unserialisable_value_is_refused(memo, value):
    """Мусор должен падать здесь, а не теряться молча на сериализации."""
    with pytest.raises(StateError) as info:
        memo.set("ключ", value)

    assert "Допустимы только" in str(info.value)


def test_unserialisable_value_is_not_written(memo):
    with pytest.raises(StateError):
        memo.set("ключ", object())

    assert memo.get("ключ") is None


@pytest.mark.parametrize("key", ["", "   ", None, 42, [], {}])
def test_bad_keys_are_refused(memo, key):
    with pytest.raises(StateError):
        memo.set(key, "значение")


def test_too_long_key_is_refused(memo):
    with pytest.raises(StateError):
        memo.set("к" * (MAX_KEY_LENGTH + 1), "значение")


def test_key_at_the_limit_is_accepted(memo):
    memo.set("к" * MAX_KEY_LENGTH, "значение")

    assert memo.get("к" * MAX_KEY_LENGTH) == "значение"


def test_checks_are_usable_on_their_own():
    assert _check_key("город") == "город"
    assert _check_value({"a": 1}) == json.dumps({"a": 1}, ensure_ascii=False)


# --- битые данные -----------------------------------------------------------


def test_corrupt_value_reads_as_absent(db):
    """Битая запись не должна ронять голосовой диалог.

    В базу могли попасть данные из прошлой версии или чужой инструмент.
    Плагин получит `default` и продолжит, а не упадёт.
    """
    memo = PluginState(db, plugin="memo")
    memo._ensure_schema()
    conn = memo._connect()
    try:
        conn.execute(
            "INSERT INTO plugin_state(plugin, key, value, updated) "
            "VALUES(?, ?, ?, ?)", ("memo", "битый", "не json", 0.0))
        conn.commit()
    finally:
        conn.close()

    assert memo.get("битый") is None
    assert memo.get("битый", "запасное") == "запасное"


def test_unreadable_database_does_not_raise(db):
    """Битая база — это «состояния нет», а не падение."""
    state = PluginState(db, plugin="memo")
    state._ensure_schema()
    # Подменяем путь на каталог: соединение упадёт.
    broken = PluginState(str(db) + "-не-база", plugin="memo")

    assert broken.get("любой") is None
    assert broken.keys() == []
    assert broken.clear() == 0
    assert state.get("любой") is None


def test_write_to_unwritable_place_does_not_raise(tmp_path):
    """Ошибка записи глушится: потерянный ключ лучше молчащего ассистента.

    Путь заведомо непроходимый: на месте каталога лежит файл. Первый
    вариант теста использовал несуществующий вложенный каталог — и
    писал туда успешно, потому что `mkdir(parents=True)` его создаёт.
    Тест был неправильным, а не код.
    """
    blocker = tmp_path / "не-каталог"
    blocker.write_text("я файл, а не папка", encoding="utf-8")
    state = PluginState(str(blocker / "plugins.db"), plugin="memo")

    state.set("город", "Москва")

    assert state.get("город") is None
    assert state.keys() == []
    assert state.delete("город") is False
    assert state.clear() == 0


def test_schema_created_on_first_use(db):
    PluginState(db, plugin="memo").set("ключ", 1)

    conn = sqlite3.connect(db)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()

    assert "plugin_state" in tables


def test_second_instance_sees_created_schema(db):
    PluginState(db, plugin="один").set("ключ", 1)

    assert PluginState(db, plugin="два").get("ключ") is None


def test_default_path_lands_in_data_dir():
    """База плагинов — рядом с остальными данными, а не в коде."""
    from sinaps.plugins.state import default_path
    from sinaps.core.config import DATA_DIR

    assert default_path().parent == DATA_DIR
    assert default_path().name == "plugins.db"