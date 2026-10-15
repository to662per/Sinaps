"""Загрузчик плагинов: папка с манифестом превращается в команды.

Здесь плагины настоящие и живут в дочерних процессах — как и задумано.
Заглушки не годились бы: главное свойство загрузчика в том, что **чужой
код не выполняется в процессе ассистента**, и проверить это можно
только по-настоящему.

Каждый тест поднимает процесс (~0.25 с). Это медленно, зато проверяет
то, ради чего система и делается. Часть набора — про разрешения и
запрет сети; без них загрузчик выглядел бы работающим, а плагин без
одобрения всё равно вышел бы в интернет.

Закрывает дыры этапа 3:
  * имя плагина проверяется на уникальность;
  * функция из `entry` проверяется на существование.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from sinaps.plugins import approvals
from sinaps.plugins.loader import LoadReport, discover, load_all, load_plugin
from sinaps.plugins.manifest import ManifestError
from sinaps.plugins.state import PluginState
from sinaps.skills.registry import SkillRegistry

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


# --- вспомогательное --------------------------------------------------------

WORKING = """\
    from sinaps.plugins.api import Response, Skill

    async def handle(ctx, slots):
        return Response(speech=f"услышал {ctx.text}",
                        display="плагин ответил", meta={"слот": slots})

    def register(registry):
        skill = Skill("get")
        skill.command("now", handle, patterns=[r"^погода$"],
                      examples=["погода"], slots={})
        registry.add(skill)
        return skill
"""


def make_plugin(folder, name="weather", *, version="1.0.0",
                permissions="  - state", entry="plugin:register",
                description="Тестовый плагин", body=None,
                extra="", module="plugin"):
    """Создать папку плагина с манифестом и кодом."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(textwrap.dedent(f"""\
        name: {name}
        version: {version}
        description: {description}
        entry: {entry}
        permissions:
        {permissions}
        {extra}"""), encoding="utf-8")
    (folder / f"{module}.py").write_text(
        textwrap.dedent(body if body is not None else WORKING),
        encoding="utf-8")
    return folder


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry()


@pytest.fixture(autouse=True)
def isolated_approvals(tmp_path, monkeypatch):
    """Одобрения — во временный каталог, боевой `data/plugins.json` не трогаем."""
    monkeypatch.setattr(approvals, "path", lambda: tmp_path / "plugins.json")


def find(registry: SkillRegistry, name: str):
    """Команда по имени.

    Без перечисления доступных имён падение выглядело бы как
    `NoneType has no attribute handler` — и пришлось бы гадать, что
    не так: плагин не загрузился, команда называется иначе или её
    съел конфликт имён.
    """
    spec = next((s for s in registry.specs if s.name == name), None)
    if spec is None:
        raise AssertionError(
            f"нет команды «{name}»; зарегистрированы: "
            f"{sorted(registry.spec_names())}")
    return spec


class FakeCtx:
    """Контекст диалога для прямого вызова обработчика."""

    def __init__(self, text="привет", **kwargs):
        self.text = text
        self.source = "voice"
        self.confidence = 1.0
        self.history: list[dict[str, str]] = []
        self.user_name = "Капитан"
        self.pending_intent = None
        for key, value in kwargs.items():
            setattr(self, key, value)


# --- нахождение (процессы не нужны) -----------------------------------------


def test_discover_finds_plugin_folders(tmp_path):
    make_plugin(tmp_path / "weather")

    assert [p.name for p in discover(tmp_path)] == ["weather"]


def test_discover_ignores_folder_without_manifest(tmp_path):
    (tmp_path / "просто папка").mkdir()

    assert discover(tmp_path) == []


def test_discover_returns_nothing_for_missing_folder(tmp_path):
    assert discover(tmp_path / "нет-такого") == []


def test_discover_order_is_alphabetical(tmp_path):
    """Порядок загрузки не зависит от порядка в каталоге."""
    for name in ("zebra", "alpha", "middle"):
        make_plugin(tmp_path / name, name=name)

    assert [p.name for p in discover(tmp_path)] == ["alpha", "middle", "zebra"]


# --- главное свойство: чужой код не идёт в нашем процессе ------------------


async def test_plugin_code_does_not_run_in_parent_process(tmp_path, registry):
    """Модуль плагина не должен появиться в процессе ассистента.

    Если бы загрузчик импортировал его сам, модуль остался бы в
    `sys.modules` — и все последующие проверки на сеть стали бы
    фикцией: код-то уже выполнился, до всяких запретов.
    """
    make_plugin(tmp_path / "sneaky", name="sneaky", module="sinaps_sneaky_probe")

    await load_all(tmp_path, registry)

    assert "sinaps_sneaky_probe" not in sys.modules, (
        "модуль плагина импортирован в процессе ассистента — "
        "код плагина выполняется там же, где живёт ядро"
    )


async def test_plugin_folder_not_left_on_sys_path(tmp_path, registry):
    """Папка плагина не должна остаться в путях нашего процесса."""
    before = list(sys.path)

    await load_all(tmp_path, registry)

    assert sys.path == before


# --- успешная загрузка ------------------------------------------------------


async def test_plugin_loads_and_registers_command(tmp_path, registry):
    folder = make_plugin(tmp_path / "weather")

    plugin = await load_plugin(folder, registry)

    assert plugin.name == "weather"
    assert plugin.commands == ("weather.get.now",)
    try:
        assert find(registry, "weather.get.now") is not None
    finally:
        await plugin.stop()


async def test_prefix_not_doubled_when_skill_named_like_plugin(tmp_path, registry):
    """Навык, названный как плагин, не даёт `weather.weather.now`."""
    body = WORKING.replace('Skill("get")', 'Skill("weather")')
    folder = make_plugin(tmp_path / "weather", body=body)

    plugin = await load_plugin(folder, registry)

    try:
        assert plugin.commands == ("weather.now",)
    finally:
        await plugin.stop()


async def test_two_plugins_do_not_collide(tmp_path, registry):
    make_plugin(tmp_path / "weather", name="weather")
    make_plugin(tmp_path / "timer", name="timer")

    report = await load_all(tmp_path, registry)
    try:
        assert [p.name for p in report.loaded] == ["timer", "weather"]
        assert set(registry.spec_names()) == {"weather.get.now", "timer.get.now"}
    finally:
        await report.stop_all()


async def test_slots_cross_the_boundary(tmp_path, registry):
    """Слоты плагина доезжают до ядра рабочими, а не только именами."""
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Response, Skill, SlotSpec

        async def handle(ctx, slots):
            return Response(speech="ок")

        def register(registry):
            skill = Skill("set")
            skill.command("timer", handle, patterns=[r"^таймер$"], slots={
                "minutes": SlotSpec(name="minutes", type="number",
                                    required=True, description="минуты"),
            })
            registry.add(skill)
            return skill
        """)
    folder = make_plugin(tmp_path / "timer", name="timer", body=body)

    plugin = await load_plugin(folder, registry)
    try:
        spec = find(registry, "timer.set.timer")
        assert spec.slots["minutes"].type == "number"
        assert spec.slots["minutes"].required is True
        assert spec.slots["minutes"].description == "минуты"
    finally:
        await plugin.stop()


# --- вызов через границу ----------------------------------------------------


async def test_call_returns_response(tmp_path, registry):
    folder = make_plugin(tmp_path / "weather")
    plugin = await load_plugin(folder, registry)

    try:
        response = await find(registry, "weather.get.now").handler(
            FakeCtx("погода"), {"city": "Москва"})

        assert response.speech == "услышал погода"
        assert response.display == "плагин ответил"
        assert response.meta == {"слот": {"city": "Москва"}}
    finally:
        await plugin.stop()


async def test_context_history_crosses_the_boundary(tmp_path, registry):
    """История диалога нужна плагину и должна доехать целой."""
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Response, Skill

        async def handle(ctx, slots):
            last = ctx.last_user_text()
            return Response(speech=f"последнее: {last}; всего {len(ctx.history)}")

        def register(registry):
            skill = Skill("get")
            skill.command("now", handle, patterns=[r"^погода$"])
            registry.add(skill)
            return skill
        """)
    folder = make_plugin(tmp_path / "weather", body=body)
    plugin = await load_plugin(folder, registry)

    try:
        ctx = FakeCtx("погода")
        ctx.history = [{"role": "user", "content": "что было"},
                       {"role": "assistant", "content": "встречаемся"}]
        response = await find(registry, "weather.get.now").handler(ctx, {})

        assert "последнее: что было" in response.speech
        assert "всего 2" in response.speech
    finally:
        await plugin.stop()


async def test_plugin_error_comes_back_with_speech(tmp_path, registry):
    """Ошибка плагина должна быть произнесена, а не съедена.

    Обработчик диалога ловит `SinapsError` и говорит `exc.speech`. Если
    через границу пройдёт голое исключение, человек услышит «что-то пошло
    не так» вместо объяснения.
    """
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Response, Skill
        from sinaps.plugins.api import NetworkDisabled

        async def handle(ctx, slots):
            raise NetworkDisabled("проверка связи")

        def register(registry):
            skill = Skill("get")
            skill.command("now", handle, patterns=[r"^погода$"])
            registry.add(skill)
            return skill
        """)
    folder = make_plugin(tmp_path / "weather", body=body)
    plugin = await load_plugin(folder, registry)

    try:
        with pytest.raises(Exception) as info:
            await find(registry, "weather.get.now").handler(FakeCtx(), {})

        # Речь едет отдельным полем, а не в тексте ошибки: текст нужен
        # человеку в логе, произносимое — вслух.
        assert "нет доступа к сети" in info.value.speech, (
            f"речь не доехала: {info.value.speech!r}"
        )
        assert "Плагин" in str(info.value), "в логе должно быть видно, чей это плагин"
    finally:
        await plugin.stop()


async def test_hanging_plugin_is_stopped(tmp_path, registry):
    """Зависший плагин обязан быть убит, а не держать диалог открытым.

    Без этого единственный способ вернуть ассистента к жизни —
    снять его руками через диспетчер задач.
    """
    body = textwrap.dedent("""\
        import asyncio
        from sinaps.plugins.api import Response, Skill

        async def handle(ctx, slots):
            await asyncio.sleep(600)
            return Response(speech="не дождались")

        def register(registry):
            skill = Skill("get")
            skill.command("now", handle, patterns=[r"^погода$"])
            registry.add(skill)
            return skill
        """)
    folder = make_plugin(tmp_path / "slow", name="slow", body=body,
                         extra="timeout_s: 1\n")
    plugin = await load_plugin(folder, registry)

    try:
        with pytest.raises(Exception) as info:
            await find(registry, "slow.get.now").handler(FakeCtx(), {})

        assert "1 с" in str(info.value), str(info.value)
        await asyncio.sleep(0.1)
        assert plugin.process is not None and not plugin.process.alive, (
            "процесс зависшего плагина не закрыт"
        )
    finally:
        await plugin.stop()


# --- поломки не роняют ------------------------------------------------------


async def test_broken_plugin_does_not_break_others(tmp_path, registry):
    make_plugin(tmp_path / "a_good", name="a_good")
    make_plugin(tmp_path / "z_broken", name="z_broken",
                body="raise RuntimeError('автор ошибся')")

    report = await load_all(tmp_path, registry)
    try:
        assert [p.name for p in report.loaded] == ["a_good"]
        assert "a_good.get.now" in registry.spec_names()
    finally:
        await report.stop_all()


async def test_duplicate_name_is_rejected(tmp_path, registry):
    make_plugin(tmp_path / "weather_v1", name="weather")
    make_plugin(tmp_path / "weather_v2", name="weather")

    report = await load_all(tmp_path, registry)
    try:
        assert [p.name for p in report.loaded] == ["weather"]
        assert report.failed[0][0] == "weather_v2"
        assert "уже загружен" in report.failed[0][1]
    finally:
        await report.stop_all()


async def test_missing_entry_function_is_reported(tmp_path, registry):
    folder = make_plugin(tmp_path / "bad", name="bad",
                         entry="plugin:no_such_function")

    with pytest.raises(ManifestError) as info:
        await load_plugin(folder, registry)

    assert "no_such_function" in str(info.value)
    assert "нет функции" in str(info.value)


async def test_broken_manifest_does_not_break_others(tmp_path, registry):
    make_plugin(tmp_path / "a_good", name="a_good")
    bad = tmp_path / "b_bad"
    bad.mkdir()
    (bad / "manifest.yaml").write_text("name: [сломан\n", encoding="utf-8")

    report = await load_all(tmp_path, registry)
    try:
        assert [p.name for p in report.loaded] == ["a_good"]
        assert report.failed[0][0] == "b_bad"
    finally:
        await report.stop_all()


async def test_failing_register_leaves_no_commands(tmp_path, registry):
    """Команды упавшего плагина не должны остаться в реестре.

    Проверяется именно то, что осталось в ядре: откат происходит в
    ребёнке, и ошибка наружу уходит одна.
    """
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            first = Skill("first")
            first.command("a", lambda ctx, slots: None, patterns=[r"^a$"])
            registry.add(first)
            second = Skill("second")
            second.command("b", lambda ctx, slots: None, patterns=[r"^b$"])
            registry.add(second)
            raise ValueError("сломался на третьей команде")
        """)
    folder = make_plugin(tmp_path / "broken", name="broken", body=body)

    with pytest.raises(ManifestError):
        await load_plugin(folder, registry)

    assert registry.spec_names() == (), (
        f"остались команды упавшего плагина: {registry.spec_names()}"
    )


async def test_register_returning_garbage_is_rejected(tmp_path, registry):
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        def register(registry):
            skill = Skill("leak")
            skill.command("a", lambda ctx, slots: None, patterns=[r"^a$"])
            registry.add(skill)
            return 42
        """)
    folder = make_plugin(tmp_path / "bad", name="bad", body=body)

    with pytest.raises(ManifestError) as info:
        await load_plugin(folder, registry)

    assert "register вернул" in str(info.value)
    assert registry.spec_names() == ()


async def test_syntax_error_is_caught(tmp_path, registry):
    make_plugin(tmp_path / "good", name="good")
    make_plugin(tmp_path / "zz_broken", name="zz_broken",
                body="def register(registry)\n    pass")

    report = await load_all(tmp_path, registry)
    try:
        assert [p.name for p in report.loaded] == ["good"]
        assert report.failed, "ошибка синтаксиса должна быть перехвачена"
    finally:
        await report.stop_all()


async def test_plugin_without_commands_is_rejected(tmp_path, registry):
    """Плагин, который ничего не зарегистрировал, — неудачная загрузка."""
    body = "def register(registry):\n    return None\n"
    folder = make_plugin(tmp_path / "empty", name="empty", body=body)

    with pytest.raises(ManifestError) as info:
        await load_plugin(folder, registry)

    assert "ни одной команды" in str(info.value)


async def test_command_conflict_loses_only_that_command(tmp_path, registry):
    """Конфликт теряет одну команду, а не весь плагин."""
    from sinaps.skills.base import Skill

    # Ядро занимает именно `weather.get.now`. Иначе имена не совпадут,
    # конфликта не случится, и тест будет зелёным вхолостую.
    core = Skill("weather.get")
    core.command("now", lambda ctx, slots: None, patterns=[r"^погода$"])
    registry.add(core)

    body = textwrap.dedent("""\
        from sinaps.plugins.api import Skill

        async def handle(ctx, slots):
            return Response(speech="ок")

        def register(registry):
            skill = Skill("get")
            skill.command("now", handle, patterns=[r"^погода$"])
            skill.command("tomorrow", handle, patterns=[r"^завтра$"])
            registry.add(skill)
            return skill
        """)
    folder = make_plugin(tmp_path / "weather", name="weather", body=body)

    plugin = await load_plugin(folder, registry)
    try:
        assert plugin.commands == ("weather.get.tomorrow",), (
            "потеряна вторая команда из-за конфликта первой"
        )
    finally:
        await plugin.stop()


# --- разрешения -------------------------------------------------------------

NET_PROBE = """\
    from sinaps.plugins.api import Response, Skill

    async def handle(ctx, slots):
        try:
            import socket
        except Exception as exc:
            return Response(speech=f"закрыто: {type(exc).__name__}")
        return Response(speech="открыто")

    def register(registry):
        skill = Skill("net")
        skill.command("try", handle, patterns=[r"^проверь сеть$"])
        registry.add(skill)
        return skill
"""


async def test_network_is_closed_without_approval(tmp_path, registry):
    """Без одобрения плагин не может открыть сокет.

    Требование проекта: ядро строго локально. Плагин без одобрения на
    сеть — тоже.
    """
    folder = make_plugin(tmp_path / "netty", name="netty", body=NET_PROBE,
                         permissions="  - network.http")

    plugin = await load_plugin(folder, registry)
    try:
        response = await find(registry, "netty.net.try").handler(FakeCtx(), {})
        assert response.speech == "закрыто: NetworkDisabled"
        assert plugin.withheld == ("network.http",)
    finally:
        await plugin.stop()


async def test_network_opens_after_approval(tmp_path, registry):
    """Одобрение действительно открывает сеть, а не только пишется в файл."""
    approvals.grant("netty", ("network.http",))
    folder = make_plugin(tmp_path / "netty", name="netty", body=NET_PROBE,
                         permissions="  - network.http")

    plugin = await load_plugin(folder, registry)
    try:
        response = await find(registry, "netty.net.try").handler(FakeCtx(), {})
        assert response.speech == "открыто"
        assert plugin.withheld == ()
    finally:
        await plugin.stop()


async def test_core_never_grants_network_by_itself(tmp_path, registry):
    """Проверка проверки: без записи в файле сеть закрыта.

    Если бы где-то в коде права выдавались сами, предыдущие тесты были
    бы бессмысленны — они проверяли бы то, что и так всегда верно.
    """
    folder = make_plugin(tmp_path / "netty", name="netty", body=NET_PROBE,
                         permissions="  - network.http")

    plugin = await load_plugin(folder, registry)
    try:
        assert not approvals.approved(), (
            "файл одобрений кто-то заполнил сам — права выдаются не тем"
        )
        assert plugin.granted == ()
    finally:
        await plugin.stop()


async def test_changed_permissions_are_withheld(tmp_path, registry):
    """Одобрение на один набор прав не действует на другой."""
    approvals.grant("netty", ("state",))
    folder = make_plugin(tmp_path / "netty", name="netty", body=NET_PROBE,
                         permissions="  - network.http")

    plugin = await load_plugin(folder, registry)
    try:
        assert plugin.granted == ()
        assert plugin.withheld == ("network.http",)
    finally:
        await plugin.stop()


async def test_approval_of_other_plugin_does_not_leak(tmp_path, registry):
    """Одобрение одного плагина не достаётся другому."""
    approvals.grant("netty", ("network.http",))
    folder = make_plugin(tmp_path / "sneak", name="sneak", body=NET_PROBE,
                         permissions="  - network.http")

    plugin = await load_plugin(folder, registry)
    try:
        assert plugin.granted == ()
    finally:
        await plugin.stop()


async def test_plugin_loads_even_without_approval(tmp_path, registry):
    """Нет одобрения — плагин всё равно грузится, право отказывает само.

    Так отзыв одного права не превращается в отказ всего плагина.
    """
    folder = make_plugin(tmp_path / "netty", name="netty", body=NET_PROBE,
                         permissions="  - network.http")

    plugin = await load_plugin(folder, registry)
    try:
        assert plugin.commands == ("netty.net.try",)
    finally:
        await plugin.stop()


# --- состояние плагина через границу ---------------------------------------

MEMO = """\
    from sinaps.plugins.api import Response, Skill

    async def bump(ctx, slots):
        n = await ctx.state.get("count", 0) + 1
        await ctx.state.set("count", n)
        return Response(speech=f"счётчик {n}")

    async def show(ctx, slots):
        return Response(speech="всё: " + repr(await ctx.state.all()))

    async def drop(ctx, slots):
        await ctx.state.clear()
        return Response(speech="стёрто")

    async def poke(ctx, slots):
        try:
            await ctx.state.get("count")
        except Exception as exc:
            return Response(speech=f"отказ: {type(exc).__name__}")
        return Response(speech="прошло")

    async def store_junk(ctx, slots):
        await ctx.state.set("мусор", object())
        return Response(speech="записал")

    def register(registry):
        skill = Skill("memo")
        skill.command("bump", bump, patterns=[r"^плюс$"])
        skill.command("show", show, patterns=[r"^покажи$"])
        skill.command("drop", drop, patterns=[r"^сброс$"])
        skill.command("poke", poke, patterns=[r"^подгляди$"])
        skill.command("junk", store_junk, patterns=[r"^мусор$"])
        registry.add(skill)
        return skill
"""


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """База состояний — во временный каталог, боевая не трогается."""
    import sinaps.plugins.state as state_mod

    target = tmp_path / "plugins.db"
    monkeypatch.setattr(state_mod, "default_path", lambda: target)
    return target


def memo_plugin(root, name="memo"):
    """Плагин с состоянием в подпапке — `discover` ищет папки, не файлы."""
    return make_plugin(root / name, name, body=MEMO, permissions="  - state")


async def test_state_works_with_approval(tmp_path, registry, state_dir):
    approvals.grant("memo", ("state",))
    memo_plugin(tmp_path)

    report = await load_all(tmp_path, registry)
    try:
        assert report.loaded[0].granted == ("state",)
        spec = find(registry, "memo.bump")
        first = await spec.handler(FakeCtx(), {})
        second = await spec.handler(FakeCtx(), {})

        assert first.speech == "счётчик 1"
        assert second.speech == "счётчик 2"
    finally:
        await report.stop_all()


async def test_state_refused_without_approval(tmp_path, registry, state_dir):
    """Нет одобрения — плагин грузится, а состояние отказывает."""
    memo_plugin(tmp_path)

    report = await load_all(tmp_path, registry)
    try:
        plugin = report.loaded[0]
        assert plugin.granted == ()
        assert plugin.withheld == ("state",)
        assert plugin.commands, "плагин должен загрузиться и без одобрения"

        response = await find(registry, "memo.poke").handler(FakeCtx(), {})
        assert response.speech == "отказ: PermissionDenied"
    finally:
        await report.stop_all()


async def test_refused_plugin_writes_nothing(tmp_path, registry, state_dir):
    """Отказ означает «не выполнено», а не «выполнено, но не покажем».

    Проверка права стоит до похода в базу. Если бы она стояла после,
    плагин продолжал бы писать в чужое пространство, просто не
    получая ответа — и проверка на глаз ничего бы не показала.
    """
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Response, Skill


        async def handle(ctx, slots):
            await ctx.state.set("взлом", "данные")
            return Response(speech="записал")


        def register(registry):
            skill = Skill("memo")
            skill.command("bump", handle, patterns=[r"^плюс$"])
            registry.add(skill)
            return skill
        """)
    make_plugin(tmp_path / "memo", "memo", body=body,
                permissions="  - state")

    report = await load_all(tmp_path, registry)
    try:
        with pytest.raises(Exception):
            await find(registry, "memo.bump").handler(FakeCtx(), {})

        assert not state_dir.exists() or PluginState(
            state_dir, plugin="memo").keys() == [], (
            "отказ в правах не помешал записи в базу")
    finally:
        await report.stop_all()


async def test_state_survives_process_restart(tmp_path, registry, state_dir):
    """Данные плагина не пропали, потому что процесс перезапустился."""
    approvals.grant("memo", ("state",))
    memo_plugin(tmp_path)

    first = await load_all(tmp_path, registry)
    await find(registry, "memo.bump").handler(FakeCtx(), {})
    await first.stop_all()

    second = SkillRegistry()
    report = await load_all(tmp_path, second)
    try:
        shown = await find(second, "memo.show").handler(FakeCtx(), {})
        assert "'count': 1" in shown.speech, (
            f"состояние не пережило перезапуск: {shown.speech}"
        )
    finally:
        await report.stop_all()


async def test_plugin_does_not_see_another_plugin_state(tmp_path,
                                                        registry,
                                                        state_dir):
    """Пространства имён раздельные — ради этого и отдельная таблица."""
    approvals.grant("memo", ("state",))
    approvals.grant("other", ("state",))
    memo_plugin(tmp_path, "memo")
    memo_plugin(tmp_path, "other")

    report = await load_all(tmp_path, registry)
    try:
        await find(registry, "memo.bump").handler(FakeCtx(), {})

        shown = await find(registry, "other.memo.show").handler(FakeCtx(), {})

        assert shown.speech == "всё: {}", (
            f"чужой плагин увидел данные соседа: {shown.speech}"
        )
    finally:
        await report.stop_all()


async def test_clear_removes_only_own_state(tmp_path, registry, state_dir):
    approvals.grant("memo", ("state",))
    approvals.grant("other", ("state",))
    memo_plugin(tmp_path, "memo")
    memo_plugin(tmp_path, "other")

    report = await load_all(tmp_path, registry)
    try:
        await find(registry, "memo.bump").handler(FakeCtx(), {})
        await find(registry, "other.memo.bump").handler(FakeCtx(), {})
        await find(registry, "memo.drop").handler(FakeCtx(), {})

        shown_other = await find(registry, "other.memo.show").handler(FakeCtx(), {})
        shown_memo = await find(registry, "memo.show").handler(FakeCtx(), {})

        assert "'count': 1" in shown_other.speech, (
            f"чужое состояние стёрли: {shown_other.speech}"
        )
        assert shown_memo.speech == "всё: {}", (
            f"своё состояние не стёрли: {shown_memo.speech}"
        )
    finally:
        await report.stop_all()


async def test_unserialisable_value_reaches_the_plugin_as_an_error(
        tmp_path, registry, state_dir):
    """Мусор падает в плагине с объяснением, а не теряется молча."""
    approvals.grant("memo", ("state",))
    memo_plugin(tmp_path)

    report = await load_all(tmp_path, registry)
    try:
        with pytest.raises(Exception) as info:
            await find(registry, "memo.junk").handler(FakeCtx(), {})

        assert "Допустимы только" in str(info.value)
    finally:
        await report.stop_all()


async def test_state_is_rejected_for_ungranted_capability(tmp_path,
                                                          registry,
                                                          state_dir):
    """Плагин не может позвать чужую возможность, даже назвав её вручную.

    Право проверяется по имени возможности, а не по тому, каким
    свойством плагин позвал. Кадр можно составить руками — и раньше
    такая попытка проходила мимо, потому что смотрели только на
    `ctx.state`.
    """
    approvals.grant("memo", ("state",))
    body = textwrap.dedent("""\
        import sys

        from sinaps.plugins.api import PluginError, Response, Skill


        async def handle(ctx, slots):
            from sinaps.plugins.protocol import read_message, write_message

            write_message(sys.stdout.buffer,
                          {"op": "cap", "cap": "files",
                           "action": "read", "kwargs": {"path": "секрет"}})
            message = read_message(sys.stdin.buffer)
            if message.get("op") == "cap_fail":
                raise PluginError(message.get("message", "отказ"))
            return Response(speech="прочитал секрет")


        def register(registry):
            skill = Skill("memo")
            skill.command("bump", handle, patterns=[r"^плюс$"])
            registry.add(skill)
            return skill
        """)
    make_plugin(tmp_path / "memo", "memo", body=body, permissions="  - state")

    report = await load_all(tmp_path, registry)
    try:
        with pytest.raises(Exception) as info:
            await find(registry, "memo.bump").handler(FakeCtx(), {})

        assert "одобрения" in str(info.value) or "files" in str(info.value)
    finally:
        await report.stop_all()

async def test_context_still_has_dialog_fields_with_state(tmp_path,
                                                          registry,
                                                          state_dir):
    """Появление `state` не должно сломать остальной контекст.

    Обёртка `_PluginContext` подменяет `DialogContext`, и легко было бы
    потерять при этом половину полей. Тест проходит по тем, что плагин
    действительно использует.
    """
    approvals.grant("memo", ("state",))
    body = textwrap.dedent("""\
        from sinaps.plugins.api import Response, Skill


        async def handle(ctx, slots):
            n = await ctx.state.get("count", 0) + 1
            await ctx.state.set("count", n)
            return Response(
                speech=f"{ctx.text}|{ctx.user_name}|{ctx.source}|{n}")


        def register(registry):
            skill = Skill("memo")
            skill.command("bump", handle, patterns=[r"^плюс$"])
            registry.add(skill)
            return skill
        """)
    make_plugin(tmp_path / "memo", "memo", body=body, permissions="  - state")

    report = await load_all(tmp_path, registry)
    try:
        response = await find(registry, "memo.bump").handler(
            FakeCtx("привет", user_name="Шкипер", source="text"), {})

        assert response.speech == "привет|Шкипер|text|1"
    finally:
        await report.stop_all()


# --- отчёт и уборка ---------------------------------------------------------


def test_report_for_empty_folder(tmp_path):
    report = LoadReport()

    assert report.ok is False
    assert report.summary() == "плагинов не найдено"


async def test_report_summarises(tmp_path, registry):
    make_plugin(tmp_path / "good", name="good")
    make_plugin(tmp_path / "bad", name="bad", body="raise RuntimeError")

    report = await load_all(tmp_path, registry)
    try:
        summary = report.summary()
        assert "загружено 1" in summary
        assert "с ошибкой 1" in summary
    finally:
        await report.stop_all()


async def test_stop_all_closes_processes(tmp_path, registry):
    make_plugin(tmp_path / "a", name="a")
    make_plugin(tmp_path / "b", name="b")

    report = await load_all(tmp_path, registry)
    assert all(p.process.alive for p in report.loaded)

    await report.stop_all()

    assert all(not p.process.alive for p in report.loaded)


async def test_reap_idle_closes_silent_processes(tmp_path, registry):
    """Простоявший процесс закрывается: ребёнок стоит 4 МБ."""
    make_plugin(tmp_path / "a", name="a")

    report = await load_all(tmp_path, registry)
    try:
        assert report.loaded[0].process.idle_for >= 0
        assert await reap(report, 0) == ["a"]
        assert not report.loaded[0].process.alive
    finally:
        await report.stop_all()


async def reap(report, idle_s: float) -> list[str]:
    from sinaps.plugins.loader import reap_idle

    return await reap_idle(report, idle_s)


async def test_call_after_idle_reap_starts_again(tmp_path, registry):
    """Поднятый заново процесс работает, а не только существует."""
    folder = make_plugin(tmp_path / "weather")
    plugin = await load_plugin(folder, registry)

    try:
        await plugin.process.stop()
        response = await find(registry, "weather.get.now").handler(
            FakeCtx("после простоя"), {})

        assert response.speech == "услышал после простоя"
        assert plugin.process.alive
    finally:
        await plugin.stop()


# --- уборка простоя ----------------------------------------------------------


async def test_busy_plugin_is_not_reaped(tmp_path, registry):
    """Занятый процесс уборка не трогает.

    Плагину разрешено работать `MAX_TIMEOUT_S` (300 с), а простой закрывают
    после 120. Одной проверки по времени не хватило бы: долгий вызов
    «стареет» всё время работы и был бы убит посреди разговора.
    """
    make_plugin(tmp_path / "a", name="a")

    report = await load_all(tmp_path, registry)
    try:
        report.loaded[0].process.busy = True

        assert await reap(report, 0) == [], "занятый процесс убран"
        assert report.loaded[0].process.alive

        report.loaded[0].process.busy = False
        assert await reap(report, 0) == ["a"], "свободный процесс не убран"
    finally:
        await report.stop_all()


async def test_busy_flag_cleared_after_call(tmp_path, registry):
    """Флаг занятости не залипает после вызова.

    Залипший флаг означал бы, что процесс больше никогда не уберут: он
    навсегда остался бы «занятым» и память не освободилась бы.
    """
    make_plugin(tmp_path / "a", name="a")

    report = await load_all(tmp_path, registry)
    try:
        await find(registry, "a.get.now").handler(FakeCtx(), {})

        assert not report.loaded[0].process.busy, "флаг занятости залип"
    finally:
        await report.stop_all()


async def test_busy_flag_cleared_after_broken_call(tmp_path, registry):
    """Флаг снимается и когда вызов упал.

    Иначе плагин, который один раз ответил не тем, больше не убирался бы
    никогда — молча, и без единой записи в журнал.
    """
    body = """\
        from sinaps.plugins.api import Response, Skill

        async def handle(ctx, slots):
            raise ValueError("плагин сломался")

        def register(registry):
            skill = Skill("get")
            skill.command("now", handle, patterns=[r"^погода$"])
            registry.add(skill)
            return skill
        """
    make_plugin(tmp_path / "boom", name="boom", body=body)

    report = await load_all(tmp_path, registry)
    try:
        with pytest.raises(Exception):
            await find(registry, "boom.get.now").handler(FakeCtx(), {})

        assert not report.loaded[0].process.busy, "флаг занятости залип"
    finally:
        await report.stop_all()


async def test_reap_idle_none_keeps_everything(tmp_path, registry):
    """`None` — выключатель: не убирать ничего.

    Нужен тем, кому +0.25 с на следующий запрос дороже памяти.
    """
    make_plugin(tmp_path / "a", name="a")

    report = await load_all(tmp_path, registry)
    try:
        assert await reap(report, None) == []
        assert report.loaded[0].process.alive
    finally:
        await report.stop_all()


async def test_plugin_respawns_after_reap_by_idle(tmp_path, registry):
    """Убранный уборкой плагин поднимается и отвечает.

    Путь целиком: уборка по простою → следующий запрос → новый процесс →
    верный ответ. Раньше проверялось только ручное `stop()`, а уборку в
    приложении никто не вызывал — и «работает после перезапуска» ничего
    не значило.
    """
    make_plugin(tmp_path / "a", name="a")

    report = await load_all(tmp_path, registry)
    try:
        assert await reap(report, 0) == ["a"]
        assert not report.loaded[0].process.alive

        response = await find(registry, "a.get.now").handler(FakeCtx(), {})

        assert response.speech == "услышал привет"
        assert report.loaded[0].process.alive
    finally:
        await report.stop_all()
