"""Загрузчик плагинов.

Правило номер одно: **ошибка плагина не роняет ассистент.** Не «мы
постараемся не уронить», а буквально: любой сбой в чужом коде ловится,
пишется в отчёт и не выходит наружу.

Здесь плагины настоящие — маленькие файлы во временных папках. Заглушки
не годились бы: загрузчик работает с файловой системой, импортом и
состоянием процесса, и именно там он спотыкается.

Закрывает две дыры, оставленные этапом 3:
  * имя плагина проверяется на уникальность;
  * функция из `entry` проверяется на существование — формат
    «модуль:функция» был проверен, а вот самой функции не было.
"""

from __future__ import annotations

import textwrap

import pytest

from sinaps.plugins.loader import LoadReport, discover, load_all, load_plugin
from sinaps.plugins.manifest import ManifestError
from sinaps.skills.base import Skill
from sinaps.skills.registry import SkillRegistry


# --- вспомогательное --------------------------------------------------------


def make_plugin(folder, name="weather", *, version="1.0.0",
                permissions="  - state", entry="plugin:register",
                description="Тестовый плагин", body=None, filename="plugin.py"):
    """Создать папку плагина с манифестом и кодом."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(textwrap.dedent(f"""\
        name: {name}
        version: {version}
        description: {description}
        entry: {entry}
        permissions:
        {permissions}
        """), encoding="utf-8")

    source = body if body is not None else textwrap.dedent("""\
        from sinaps.plugins.api import Response, Skill

        def register(registry):
            skill = Skill("get", description="Погода")
            skill.command("now", lambda ctx, slots: None,
                          patterns=[r"^погода сейчас$"])
            registry.add(skill)
            return skill
        """)
    (folder / filename).write_text(source, encoding="utf-8")
    return folder


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry()


# --- нахождение -------------------------------------------------------------


def test_discover_finds_plugin_folders(tmp_path, registry):
    make_plugin(tmp_path / "weather")

    assert [p.name for p in discover(tmp_path)] == ["weather"]


def test_discover_ignores_folder_without_manifest(tmp_path):
    (tmp_path / "просто папка").mkdir()

    assert discover(tmp_path) == []


def test_discover_returns_nothing_for_missing_folder(tmp_path):
    assert discover(tmp_path / "нет-такого") == []


def test_discover_order_is_alphabetical(tmp_path):
    """Порядок загрузки не должен зависеть от порядка в каталоге."""
    for name in ("zebra", "alpha", "middle"):
        make_plugin(tmp_path / name, name=name)

    assert [p.name for p in discover(tmp_path)] == ["alpha", "middle", "zebra"]


# --- успешная загрузка ------------------------------------------------------


def test_plugin_loads_and_registers_command(tmp_path, registry):
    folder = make_plugin(tmp_path / "weather")

    plugin = load_plugin(folder, registry)

    assert plugin.name == "weather"
    assert "weather.get.now" in registry.spec_names()


def test_command_gets_prefix_automatically(tmp_path, registry):
    """Плагин не может «забыть» префикс: его некуда вписать."""
    folder = make_plugin(tmp_path / "weather")

    load_plugin(folder, registry)

    names = registry.spec_names()
    assert names == ("weather.get.now",), (
        f"команда без префикса ядра: {names}. Префикс ставит загрузчик."
    )


def test_prefix_added_to_each_command(tmp_path, registry):
    """Навык может называться как угодно — префикс всё равно один."""
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            skill = Skill("weather")
            skill.command("get", lambda ctx, slots: None,
                          patterns=[r"^погода$"])
            skill.command("set", lambda ctx, slots: None,
                          patterns=[r"^город$"])
            registry.add(skill)
            return skill
        """)
    folder = make_plugin(tmp_path / "p", name="p", body=body)

    load_plugin(folder, registry)

    # Навык назван «weather» при плагине «p»: префикс нужен один,
    # и «p.weather.get» здесь и есть «weather.get» с префиксом плагина.
    assert set(registry.spec_names()) == {"p.weather.get", "p.weather.set"}


def test_prefix_not_doubled_when_skill_named_like_plugin(tmp_path, registry):
    """Навык с именем плагина не должен дать `weather.weather.get`."""
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            skill = Skill("weather")
            skill.command("now", lambda ctx, slots: None,
                          patterns=[r"^погода$"])
            registry.add(skill)
            return skill
        """)
    folder = make_plugin(tmp_path / "weather", name="weather", body=body)

    load_plugin(folder, registry)

    assert registry.spec_names() == ("weather.now",)


def test_two_plugins_do_not_collide(tmp_path, registry):
    make_plugin(tmp_path / "weather", name="weather")
    make_plugin(tmp_path / "timer", name="timer")

    report = load_all(tmp_path, registry)

    assert [p.name for p in report.loaded] == ["timer", "weather"]
    assert set(registry.spec_names()) == {"weather.get.now", "timer.get.now"}


def test_plugin_returning_list_of_skills(tmp_path, registry):
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            first = Skill("one")
            second = Skill("two")
            first.command("start", lambda ctx, slots: None,
                          patterns=[r"^старт$"])
            second.command("stop", lambda ctx, slots: None,
                           patterns=[r"^стоп$"])
            for skill in (first, second):
                registry.add(skill)
            return [first, second]
        """)
    folder = make_plugin(tmp_path / "multi", name="multi", body=body)

    plugin = load_plugin(folder, registry)

    assert set(plugin.commands) == {"multi.one.start", "multi.two.stop"}


# --- уникальность имени (дыра этапа 3) --------------------------------------


def test_duplicate_name_is_rejected(tmp_path, registry):
    """ТЗ требует: имя должно быть не занято другим плагином."""
    make_plugin(tmp_path / "weather_v1", name="weather")
    make_plugin(tmp_path / "weather_v2", name="weather")

    report = load_all(tmp_path, registry)

    assert [p.name for p in report.loaded] == ["weather"]
    assert report.failed, "второй плагин с тем же именем должен быть отклонён"
    assert "уже загружен" in report.failed[0][1]


def test_duplicate_error_names_both_folders(tmp_path, registry):
    """Человек должен понять, какой именно плагин лишний."""
    make_plugin(tmp_path / "weather_v1", name="weather")
    make_plugin(tmp_path / "weather_v2", name="weather")

    report = load_all(tmp_path, registry)

    assert report.failed[0][0] == "weather_v2"


# --- существование функции (дыра этапа 3) -----------------------------------


def test_missing_entry_function_is_reported(tmp_path, registry):
    """Формат «модуль:функция» проверялся; сама функция — нет."""
    folder = make_plugin(tmp_path / "bad", name="bad",
                         entry="plugin:no_such_function")

    with pytest.raises(ManifestError) as info:
        load_plugin(folder, registry)

    message = str(info.value)
    assert "no_such_function" in message
    assert "нет функции" in message


def test_missing_entry_module_is_reported(tmp_path, registry):
    folder = make_plugin(tmp_path / "bad", name="bad", entry="нетакой:reg")

    with pytest.raises(ManifestError):
        load_plugin(folder, registry)


def test_entry_must_be_callable(tmp_path, registry):
    body = textwrap.dedent("""\
        register = "не функция"
        """)
    folder = make_plugin(tmp_path / "bad", name="bad", body=body)

    with pytest.raises(ManifestError) as info:
        load_plugin(folder, registry)

    assert "не функция" in str(info.value)


# --- поломки не роняют ------------------------------------------------------


def test_broken_plugin_does_not_break_others(tmp_path, registry):
    make_plugin(tmp_path / "a_good", name="a_good")
    make_plugin(tmp_path / "z_broken", name="z_broken",
                body="raise RuntimeError('автор ошибся')")

    report = load_all(tmp_path, registry)

    assert [p.name for p in report.loaded] == ["a_good"]
    assert "a_good.get.now" in registry.spec_names()


def test_failing_register_rolls_back_its_commands(tmp_path, registry):
    """Команды упавшего плагина не должны остаться в реестре."""
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            first = Skill("first")
            registry.add(first)
            first.command("a", lambda ctx, slots: None, patterns=[r"^a$"])
            second = Skill("second")
            registry.add(second)
            second.command("b", lambda ctx, slots: None, patterns=[r"^b$"])
            raise ValueError("сломался на третьей команде")
        """)
    folder = make_plugin(tmp_path / "broken", name="broken", body=body)

    with pytest.raises(ManifestError):
        load_plugin(folder, registry)

    assert registry.spec_names() == (), (
        f"остались команды упавшего плагина: {registry.spec_names()}"
    )
    assert registry.skills == []


def test_rollback_does_not_remove_core_skills(tmp_path, registry):
    """Откат не должен трогать то, что было до плагина."""
    core = Skill("media")
    core.command("up", lambda ctx, slots: None, patterns=[r"^громче$"])
    registry.add(core)

    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            skill = Skill("x")
            registry.add(skill)
            raise RuntimeError("упал")
        """)
    folder = make_plugin(tmp_path / "broken", name="broken", body=body)

    with pytest.raises(ManifestError):
        load_plugin(folder, registry)

    assert registry.spec_names() == ("media.up",), (
        "откат снёс собственные навыки ядра"
    )


def test_broken_manifest_does_not_break_others(tmp_path, registry):
    make_plugin(tmp_path / "a_good", name="a_good")
    bad = tmp_path / "b_bad"
    bad.mkdir()
    (bad / "manifest.yaml").write_text("name: [сломан\n", encoding="utf-8")

    report = load_all(tmp_path, registry)

    assert [p.name for p in report.loaded] == ["a_good"]
    assert report.failed[0][0] == "b_bad"


def test_bad_entry_type_is_reported(tmp_path, registry):
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            return "просто строка"
        """)
    folder = make_plugin(tmp_path / "bad", name="bad", body=body)

    with pytest.raises(ManifestError) as info:
        load_plugin(folder, registry)

    assert "register вернул" in str(info.value)


def test_register_returning_garbage_is_rolled_back(tmp_path, registry):
    """Мусор на выходе — тоже поломка, и тоже с откатом."""
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            skill = Skill("leak")
            registry.add(skill)
            return 42
        """)
    folder = make_plugin(tmp_path / "bad", name="bad", body=body)

    with pytest.raises(ManifestError):
        load_plugin(folder, registry)

    assert registry.spec_names() == ()


def test_syntax_error_in_plugin_is_caught(tmp_path, registry):
    make_plugin(tmp_path / "good", name="good")
    make_plugin(tmp_path / "zz_broken", name="zz_broken",
                body="def register(registry)\n    pass")

    report = load_all(tmp_path, registry)

    assert [p.name for p in report.loaded] == ["good"]
    assert report.failed, "ошибка синтаксиса должна быть перехвачена"


# --- конфликт команд --------------------------------------------------------


def test_command_conflict_loses_only_that_command(tmp_path, registry):
    """Конфликт не роняет плагин: теряется одна команда из нескольких."""
    core = Skill("weather")
    core.command("get", lambda ctx, slots: None, patterns=[r"^погода$"])
    registry.add(core)

    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            skill = Skill("get")
            registry.add(skill)
            skill.command("a", lambda ctx, slots: None, patterns=[r"^погода$"])
            skill.command("b", lambda ctx, slots: None, patterns=[r"^прогноз$"])
            return skill
        """)
    folder = make_plugin(tmp_path / "weather", name="weather", body=body)

    plugin = load_plugin(folder, registry)

    assert "weather.get.a" in plugin.commands
    assert "weather.get.b" in plugin.commands, (
        "вторая команда потеряна из-за конфликта первой — "
        "плагин должен терять только конфликтующую"
    )


# --- отчёт ------------------------------------------------------------------


def test_report_summarises(tmp_path, registry):
    make_plugin(tmp_path / "good", name="good")
    make_plugin(tmp_path / "bad", name="bad", body="raise RuntimeError")

    report = load_all(tmp_path, registry)

    summary = report.summary()
    assert "загружено 1" in summary
    assert "с ошибкой 1" in summary


def test_report_for_empty_folder(tmp_path, registry):
    report = load_all(tmp_path, registry)

    assert report.loaded == []
    assert report.summary() == "плагинов не найдено"
    assert report.ok is False


def test_failed_plugin_records_folder_and_reason(tmp_path, registry):
    make_plugin(tmp_path / "bad", name="bad", body="raise RuntimeError('х' )")

    report = load_all(tmp_path, registry)

    folder, reason = report.failed[0]
    assert folder == "bad"
    assert "RuntimeError" in reason


# --- изоляция пространства имён ---------------------------------------------


def test_same_filename_in_two_plugins(tmp_path, registry):
    """Два плагина с файлом `plugin.py` не должны перетирать друг друга."""
    first = make_plugin(
        tmp_path / "first", name="first",
        body=textwrap.dedent("""\
            from sinaps.plugins.api import Skill

            NAME = "первый"

            def register(registry):
                skill = Skill("get")
                skill.command("now", lambda ctx, slots: None,
                              patterns=[r"^первый$"])
                registry.add(skill)
                return skill
            """))
    make_plugin(
        tmp_path / "second", name="second",
        body=textwrap.dedent("""\
            from sinaps.plugins.api import Skill

            NAME = "второй"

            def register(registry):
                skill = Skill("get")
                skill.command("now", lambda ctx, slots: None,
                              patterns=[r"^второй$"])
                registry.add(skill)
                return skill
            """))

    report = load_all(tmp_path, registry)

    assert len(report.loaded) == 2, (
        f"загружено {len(report.loaded)}: {report.failed}. "
        "Одинаковое имя файла у двух плагинов не должно мешать."
    )
    assert set(registry.spec_names()) == {"first.get.now", "second.get.now"}


def test_plugin_folder_not_left_on_sys_path(tmp_path, registry):
    """Папка плагина убирается из пути сразу: иначе импорт подхватит
    модули чужих плагинов."""
    import sys

    folder = make_plugin(tmp_path / "weather")
    before = list(sys.path)

    load_plugin(folder, registry)

    assert sys.path == before, "папка плагина осталась в sys.path"


# --- реестр: метод remove ---------------------------------------------------


def test_remove_takes_skill_and_its_commands():
    reg = SkillRegistry()
    skill = Skill("x")
    skill.command("a", lambda ctx, slots: None, patterns=[r"^a$"])
    reg.add(skill)

    assert reg.remove(skill) is True
    assert reg.spec_names() == ()
    assert reg.skills == []


def test_remove_rebuilds_patterns():
    """После удаления команда не должна находиться шаблоном."""
    import re

    reg = SkillRegistry()
    keep = Skill("keep")
    keep.command("a", lambda ctx, slots: None, patterns=[r"^оставить$"])
    drop = Skill("drop")
    drop.command("b", lambda ctx, slots: None, patterns=[r"^убрать$"])
    reg.add_all([keep, drop])

    reg.remove(drop)

    found = [spec.name for pattern, spec in reg._patterns  # noqa: SLF001
             if pattern.match("убрать")]
    assert not found, f"команда удалённого навыка всё ещё находится: {found}"


def test_remove_absent_skill_is_not_an_error():
    reg = SkillRegistry()

    assert reg.remove(Skill("нет такого")) is False


def test_empty_report_object():
    """Пустой отчёт должен быть пригоден, а не падать при обращении."""
    report = LoadReport()

    assert report.ok is False
    assert report.loaded == []
    assert report.failed == []






