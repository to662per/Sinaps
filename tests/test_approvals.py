"""Кто на что согласен.

Центральное свойство здесь одно: **одобрение переживает ровно тот
набор прав, который человек видел, и ни один больше**.

Без хеша дыра была бы простой. Автор пишет в манифест `state`, ему
одобряют, потом дописывает `network.http` — а одобрение то же самое,
и права выросли незаметно. Тест `test_changing_permissions_invalidates`
существует именно ради этого.
"""

from __future__ import annotations

import json

import pytest

from sinaps.plugins import approvals


@pytest.fixture(autouse=True)
def isolated_file(tmp_path, monkeypatch):
    """Боевой `data/plugins.json` тесты не трогают.

    Подмена именно функции `path`, а не переменной: `load` и `save`
    вызывают её, и подмена работает для всех вызовов.
    """
    target = tmp_path / "plugins.json"
    monkeypatch.setattr(approvals, "path", lambda: target)
    return target


# --- пусто ------------------------------------------------------------------


def test_without_file_nothing_is_granted():
    """Плагин без файла одобрений не получает ничего."""
    assert approvals.granted_for("weather", ("network.http",)) == ()
    assert approvals.approved() == {}


def test_hash_is_stable_and_short():
    first = approvals.permissions_hash(["network.http", "state"])
    second = approvals.permissions_hash(["state", "network.http"])

    assert first == second, "порядок в манифесте не должен ломать одобрение"
    assert len(first) == 16


# --- выдача ----------------------------------------------------------------


def test_grant_gives_exactly_what_was_asked():
    approvals.grant("weather", ("network.http",))

    assert approvals.granted_for("weather", ("network.http",)) == ("network.http",)


def test_grant_writes_file_readable_by_hand():
    approvals.grant("weather", ("network.http", "state"))

    data = json.loads(approvals.path().read_text(encoding="utf-8"))
    entry = data["plugins"]["weather"]
    assert entry["permissions"] == ["network.http", "state"]
    assert entry["permissions_hash"] == approvals.permissions_hash(
        ("network.http", "state"))
    assert entry["granted_at"], "должно быть видно, когда одобрили"


def test_empty_grant_is_allowed():
    """Плагин без прав — обычное дело, файл всё равно пишется."""
    approvals.grant("notes", ())

    assert approvals.granted_for("notes", ()) == ()
    assert "notes" in approvals.approved()


# --- хеш: главное свойство --------------------------------------------------


def test_changing_permissions_invalidates_approval():
    """Добавили право после одобрения — одобрение больше не действует."""
    approvals.grant("weather", ("state",))

    assert approvals.granted_for("weather", ("state", "network.http")) == (), (
        "одобрение пережило изменение манифеста — дыра, ради которой "
        "и сделан хеш"
    )


def test_removing_permissions_also_invalidates():
    approvals.grant("weather", ("state", "network.http"))

    assert approvals.granted_for("weather", ("state",)) == ()


def test_reordered_permissions_keep_approval():
    """Перестановка прав в манифесте — не повод спрашивать заново."""
    approvals.grant("weather", ("network.http", "state"))

    assert approvals.granted_for("weather", ("state", "network.http")) == (
        "network.http", "state")


def test_edited_description_keeps_approval():
    """Описание не входит в хеш: человек видел те же права."""
    approvals.grant("weather", ("network.http",))

    assert approvals.granted_for("weather", ("network.http",)) == ("network.http",)


def test_extra_rights_in_file_are_not_handed_out():
    """Если в файл дописали права, которых плагин не объявляет, — не выдаём."""
    approvals.grant("weather", ("network.http",))
    data = json.loads(approvals.path().read_text(encoding="utf-8"))
    data["plugins"]["weather"]["permissions"].append("platform.power")
    approvals.path().write_text(json.dumps(data), encoding="utf-8")

    assert approvals.granted_for("weather", ("network.http",)) == ("network.http",)


# --- отзыв ------------------------------------------------------------------


def test_revoke_removes_approval():
    approvals.grant("weather", ("network.http",))

    assert approvals.revoke("weather") is True
    assert approvals.granted_for("weather", ("network.http",)) == ()


def test_revoke_without_approval_is_not_an_error():
    """Отзывать нечего — не беда, а не поломка."""
    assert approvals.revoke("нет такого") is False


def test_revoke_of_one_plugin_leaves_others():
    approvals.grant("weather", ("network.http",))
    approvals.grant("notes", ("state",))

    approvals.revoke("weather")

    assert approvals.granted_for("notes", ("state",)) == ("state",)


# --- битый файл -------------------------------------------------------------


def test_corrupt_file_gives_no_permissions():
    """Битый файл — это «ничего не одобрено», а не «одобрить всё»."""
    approvals.path().write_text("{это не json", encoding="utf-8")

    assert approvals.granted_for("weather", ("network.http",)) == ()


def test_wrong_shape_gives_no_permissions():
    approvals.path().write_text("[1, 2, 3]", encoding="utf-8")

    assert approvals.granted_for("weather", ("network.http",)) == ()


def test_entry_of_wrong_type_is_ignored():
    approvals.path().write_text(
        json.dumps({"version": 1, "plugins": {"weather": "всё хорошо"}}),
        encoding="utf-8")

    assert approvals.granted_for("weather", ("network.http",)) == ()


def test_broken_file_can_be_repaired_by_granting():
    """После поломки файл не должен оставаться в состоянии «нечитаемо»."""
    approvals.path().write_text("{это не json", encoding="utf-8")

    approvals.grant("weather", ("network.http",))

    assert approvals.granted_for("weather", ("network.http",)) == ("network.http",)


# --- чего не хватает -------------------------------------------------------


def test_missing_approval_is_empty_when_set_matches():
    approvals.grant("weather", ("state", "network.http"))

    assert approvals.missing_approval(
        "weather", ("state", "network.http")) == ()


def test_changed_set_asks_for_everything_again():
    """Изменился набор — спрашиваем всё, а не только новое.

    Можно было бы оставить прежние права в силе и спросить только про
    добавленные. Это выглядит вежливее, но означало бы, что одобрение
    человека переживает изменение того, что он одобрял. Строгое
    поведение стоит одного лишнего вопроса.
    """
    approvals.grant("weather", ("state",))

    assert approvals.missing_approval(
        "weather", ("state", "network.http")) == ("network.http", "state")


def test_missing_approval_for_unknown_plugin_is_everything():
    assert approvals.missing_approval(
        "new", ("network.http", "state")) == ("network.http", "state")


def test_approved_lists_everything():
    approvals.grant("weather", ("network.http",))
    approvals.grant("notes", ("state",))

    assert approvals.approved() == {
        "weather": ["network.http"], "notes": ["state"]}