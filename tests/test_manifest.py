"""Манифест плагина: пять обязательных полей и всё, что с ними связано.

Зачем манифест, если есть код
-----------------------------
Чтобы человек открыл один файл и понял, что плагин собирается делать и
к чему получит доступ, не читая код. Обещание проверяемое: всё, что
плагин реально делает, покрыто разрешениями из манифеста, а ядро не
даёт ему больше.

Потолок намеренно низкий: пять обязательных полей. Опасность этого
модуля — не в сложности, а в застревании: «а что если добавить
`depends_on`?» — и две недели вместо работы. Проверка ниже следит за
тем, чтобы список не начал расти сам собой.
"""

from __future__ import annotations

import pytest

from sinaps.plugins.manifest import (API_VERSION, DEFAULT_TIMEOUT_S,
                                     KNOWN_PERMISSIONS, MAX_TIMEOUT_S,
                                     REQUIRED_FIELDS, Manifest,
                                     ManifestError, load_manifest,
                                     parse_manifest)

#: Минимальный корректный манифест — база для проверок.
GOOD = {
    "name": "weather",
    "version": "1.0.0",
    "description": "Погода из open-meteo",
    "entry": "plugin:register",
    "permissions": ["network.http"],
}


def good(**overrides) -> dict:
    data = dict(GOOD)
    data.update(overrides)
    return data


# --- минимальный набор ------------------------------------------------------


def test_minimal_manifest_is_accepted():
    manifest = parse_manifest(good())

    assert manifest.name == "weather"
    assert manifest.module == "plugin"
    assert manifest.function == "register"


def test_five_required_fields_and_no_more():
    """Обязательных полей ровно пять.

    Список не должен расти: каждый новый обязательный параметр заставит
    автора писать его в файл, который он читает впервые. Необязательные
    вещи — необязательны.
    """
    assert REQUIRED_FIELDS == ("name", "version", "description", "entry",
                               "permissions")


def test_version_is_required():
    """Версия обязательна, хотя сравнивать её пока не с чем.

    Плагин один, но номер стоит рядом с именем при описании, и глазу
    спокойнее, когда видно «1.0.0», а не пустое место. Первая версия
    кода разрешала отсутствие и подставляла «0» — это выглядело
    мягче, но означало, что в описании плагина будет написано «0»,
    будто он черновик.
    """
    data = good()
    del data["version"]

    with pytest.raises(ManifestError) as info:
        parse_manifest(data)

    assert "version" in str(info.value)


def test_author_and_license_are_optional():
    """Они не меняют ничего в работе плагина.

    Проверяется не удалением полей, а отсутствием: в минимальном
    манифесте их нет вовсе, и это нормально.
    """
    assert "author" not in GOOD
    assert "license" not in GOOD

    manifest = parse_manifest(good())

    assert manifest.author == ""
    assert manifest.license == ""


# --- имя --------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "Погода",       # кириллица
    "Weather Plugin",   # пробел
    "1weather",     # не с буквы
    "my-plugin",    # дефис
    "",             # пусто
    "weather!",     # мусорный символ
])
def test_bad_names_are_rejected(name):
    """Имя становится префиксом команд и попадает в логи и историю."""
    with pytest.raises(ManifestError):
        parse_manifest(good(name=name))


@pytest.mark.parametrize("name", ["a", "weather", "my_plugin", "p2", "a1_b2"])
def test_good_names_are_accepted(name):
    assert parse_manifest(good(name=name)).name == name


def test_name_error_explains_the_rule():
    """Ошибка должна говорить, как правильно, а не только что не так."""
    with pytest.raises(ManifestError) as info:
        parse_manifest(good(name="Погода"))

    message = str(info.value)
    assert "латиница" in message
    assert "подчёркивание" in message


# --- версия -----------------------------------------------------------------


@pytest.mark.parametrize("version", ["1.0.0", "0.3", "2", 1, 1.0])
def test_versions_accepted(version):
    assert parse_manifest(good(version=version))


@pytest.mark.parametrize("version", ["v1.0", "бета", "1.0.0.0", "-1"])
def test_bad_versions_rejected(version):
    with pytest.raises(ManifestError):
        parse_manifest(good(version=version))


# --- точка входа ------------------------------------------------------------


@pytest.mark.parametrize("entry", ["plugin:register", "_pkg.mod:build",
                                   "a:b", "pkg.sub.mod:go"])
def test_entries_accepted(entry):
    manifest = parse_manifest(good(entry=entry))

    assert f"{manifest.module}:{manifest.function}" == entry


@pytest.mark.parametrize("entry", [
    "register",           # без модуля
    "plugin:",            # без функции
    ":register",          # без модуля
    "1plugin:register",   # модуль не может начинаться с цифры
    "plugin module:reg",  # пробел
])
def test_bad_entries_rejected(entry):
    with pytest.raises(ManifestError):
        parse_manifest(good(entry=entry))


def test_entry_error_shows_the_format():
    with pytest.raises(ManifestError) as info:
        parse_manifest(good(entry="register"))

    assert "модуль:функция" in str(info.value)


# --- разрешения -------------------------------------------------------------


def test_permissions_must_be_known():
    """Неизвестное разрешение — опечатка, а не новая возможность.

    Если бы мы молча принимали что угодно, плагин просил бы
    `network.https`, думая, что получил доступ, а не получил бы
    ничего. Хуже того: опечатку в названии разрешения легко не заметить.
    """
    with pytest.raises(ManifestError) as info:
        parse_manifest(good(permissions=["network.https"]))

    message = str(info.value)
    assert "неизвестное разрешение" in message.lower()
    assert "network.http" in message, "не подсказано, что было в виду"


def test_empty_permissions_must_be_written_explicitly():
    """Плагин без прав должен написать пустой список явно.

    Отсутствующее поле и пустой список — разные вещи. Первое означает
    «автор не подумал», второе «автор подумал и не просит ничего».
    Проверка приходит раньше остальных и называет все не хватающие
    поля сразу.
    """
    data = good()
    del data["permissions"]

    with pytest.raises(ManifestError) as info:
        parse_manifest(data)

    message = str(info.value)
    assert "permissions" in message
    assert "не хватает обязательных полей" in message


def test_null_permissions_rejected_too():
    """`permissions:` без ничего — это неявное отсутствие, а не отказ.

    Автор дописал поле, но забыл наполнить. Принимать такое нельзя:
    выглядит как «запрашивает всё», а на деле не запрашивает ничего.
    """
    with pytest.raises(ManifestError):
        parse_manifest(good(permissions=None))


def test_empty_list_is_allowed():
    manifest = parse_manifest(good(permissions=[]))

    assert manifest.permissions == ()
    assert manifest.describe_permissions() == "ничего не запрашивает"


def test_permissions_as_string_are_split():
    """Частая описка: перечислили через запятую одной строкой."""
    manifest = parse_manifest(good(permissions="files.read, state"))

    assert set(manifest.permissions) == {"files.read", "state"}


def test_duplicate_permissions_collapse():
    manifest = parse_manifest(good(permissions=["state", "state"]))

    assert manifest.permissions == ("state",)


def test_notable_permissions_are_detected():
    """Плагин, меняющий то, что человек видит, ставится не молча."""
    quiet = parse_manifest(good(permissions=["state"]))
    loud = parse_manifest(good(permissions=["platform.power"]))

    assert quiet.asks_for_attention is False
    assert loud.asks_for_attention is True


# --- версия API -------------------------------------------------------------


def test_api_version_is_optional():
    assert parse_manifest(good()).api_version is None


def test_api_version_matches_when_given():
    assert parse_manifest(good(api_version=API_VERSION)).api_version == API_VERSION


def test_api_version_from_the_future_is_rejected():
    with pytest.raises(ManifestError) as info:
        parse_manifest(good(api_version=API_VERSION + 1))

    assert "будущего" in str(info.value)


def test_api_version_must_be_number():
    with pytest.raises(ManifestError):
        parse_manifest(good(api_version="один"))


# --- таймаут ----------------------------------------------------------------


def test_timeout_defaults():
    assert parse_manifest(good()).timeout_s == DEFAULT_TIMEOUT_S


def test_timeout_is_capped():
    """Плагин не должен держать ассистент дольше пяти минут."""
    with pytest.raises(ManifestError) as info:
        parse_manifest(good(timeout_s=MAX_TIMEOUT_S + 1))

    assert str(MAX_TIMEOUT_S) in str(info.value)


def test_timeout_must_be_positive():
    with pytest.raises(ManifestError):
        parse_manifest(good(timeout_s=0))


def test_timeout_must_be_number():
    with pytest.raises(ManifestError):
        parse_manifest(good(timeout_s="долго"))


# --- неизвестные поля -------------------------------------------------------


def test_unknown_fields_are_not_an_error():
    """Плагин может быть написан под будущую версию ядра.

    Неизнакомое поле — повод предупредить, а не отказать: автор знает,
    что он делает, и мы пока просто не понимаем.
    """
    manifest = parse_manifest(good(some_future_field="что-то"))

    assert "some_future_field" in manifest.unknown_fields
    assert manifest.name == "weather"


def test_missing_required_fields_listed_at_once():
    """Человек должен увидеть все не хватающие поля, а не по одному."""
    with pytest.raises(ManifestError) as info:
        parse_manifest({"name": "weather"})

    message = str(info.value)
    for field in ("version", "description", "entry", "permissions"):
        assert field in message


# --- файл -------------------------------------------------------------------


def test_load_manifest_from_file(tmp_path):
    file = tmp_path / "manifest.yaml"
    file.write_text(
        "name: weather\n"
        "version: 1.0.0\n"
        "description: Погода\n"
        "entry: plugin:register\n"
        "permissions:\n"
        "  - network.http\n",
        encoding="utf-8",
    )

    manifest = load_manifest(file)

    assert manifest.name == "weather"
    assert manifest.permissions == ("network.http",)
    assert manifest.path == file


def test_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(ManifestError) as info:
        load_manifest(tmp_path / "нет.yaml")

    assert "не найден" in str(info.value).lower()


def test_broken_yaml_is_reported_with_path(tmp_path):
    file = tmp_path / "manifest.yaml"
    file.write_text("name: [не закрытая скобка\n", encoding="utf-8")

    with pytest.raises(ManifestError) as info:
        load_manifest(file)

    message = str(info.value)
    assert str(file) in message, "не сказано, какой именно файл"
    assert "YAML" in message


def test_yaml_that_is_not_a_mapping(tmp_path):
    """Список вместо словаря — частая ошибка отступа."""
    file = tmp_path / "manifest.yaml"
    file.write_text("- name: weather\n- version: 1.0.0\n", encoding="utf-8")

    with pytest.raises(ManifestError) as info:
        load_manifest(file)

    assert "словарь" in str(info.value)


# --- валидатор проверяет сам себя ------------------------------------------


def test_every_permission_is_documented():
    """Новое разрешение обязано где-то быть описано.

    Без этого через полгода в коде будет разрешение `files.read`, про
    которое никто не помнит, что оно даёт.
    """
    source = (__import__("pathlib").Path(__file__).resolve().parent.parent
              / "sinaps" / "plugins" / "manifest.py").read_text(encoding="utf-8")

    for permission in sorted(KNOWN_PERMISSIONS):
        assert f'"{permission}"' in source, (
            f"разрешение {permission} есть в списке, но не описано "
            "рядом с ним комментарием"
        )


def test_required_fields_are_all_validated():
    """Каждое обязательное поле проверяется, а не только перечислено.

    Поле, которое проверяется «по счастливому случаю», однажды
    пропустит мусор, и плагин сломается в худший момент.
    """
    broken = {
        "name": "Погода",           # не проходит name
        "version": "бета",         # не проходит version
        "description": "",          # не проходит description
        "entry": "register",        # не проходит entry
        "permissions": ["нет-такого"],  # не проходит permissions
    }

    for field, value in broken.items():
        with pytest.raises(ManifestError, match=None):
            parse_manifest(good(**{field: value}))


def test_manifest_is_immutable():
    """Манифест — описание того, что заявил автор, и меняться не должен.

    Иначе прокси разрешений мог бы быть обойдён подменой полей на
    лету.
    """
    manifest = parse_manifest(good())

    with pytest.raises(Exception):
        manifest.permissions = ("files.write",)  # type: ignore[misc]
