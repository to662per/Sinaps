"""Обвязка для проверки плагинов.

Здесь проверяется то, на что автор плагина будет опираться:

  * `run_phrase` действительно гоняет фразу через **настоящий**
    сопоставитель реестра, а не через свой разбор;
  * `FakePlatform` записывает вызовы и не трогает машину;
  * обвязка не оставляет за собой ни файлов, ни процессов.

Отдельно проверяется, что прогон фразы ловит промах. Обвязка, которая
на непонятную фразу возвращает пустой ответ, выглядит работающей и
проверяет саму себя.
"""

from __future__ import annotations

import textwrap

import pytest

from sinaps.platform.base import PowerAction
from sinaps.plugins.testing import (FakePlatform, PhraseNotMatched,
                                    make_ctx, plugin_under_test, run_phrase)
from sinaps.skills.base import Response, Skill
from sinaps.skills.registry import SkillRegistry

PLUGIN = textwrap.dedent("""\
    from sinaps.plugins.api import Response, Skill, SlotSpec


    async def handle(ctx, slots):
        return Response(speech="услышал " + str(slots.get("text")),
                        display="записано", meta={"слоты": slots})


    def register(registry):
        skill = Skill("memo")
        skill.command("say", handle, patterns=[r"^скажи (?P<text>.+)$"],
                      slots={"text": SlotSpec(name="text", type="string",
                                              required=True)})
        registry.add(skill)
        return skill
    """)

STATE_PLUGIN = textwrap.dedent("""\
    from sinaps.plugins.api import Response, Skill


    async def handle(ctx, slots):
        n = await ctx.state.get("n", 0) + 1
        await ctx.state.set("n", n)
        return Response(speech=f"счётчик {n}")


    def register(registry):
        skill = Skill("memo")
        skill.command("bump", handle, patterns=[r"^плюс$"])
        registry.add(skill)
        return skill
    """)


def make_folder(tmp_path, name="memo", body=PLUGIN,
                permissions="  - state"):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(textwrap.dedent(f"""\
        name: {name}
        version: 1.0.0
        description: Плагин для тестов
        entry: plugin:register
        permissions:
        {permissions}
        """), encoding="utf-8")
    (folder / "plugin.py").write_text(body, encoding="utf-8")
    return folder


# --- run_phrase -------------------------------------------------------------


async def test_phrase_reaches_the_handler():
    registry = SkillRegistry()
    skill = Skill("memo")
    skill.command("say", _echo, patterns=[r"^скажи (?P<text>.+)$"],
                  slots={"text": _slot("text")})
    registry.add(skill)

    response = await run_phrase(registry, "скажи привет")

    assert response.speech == "услышал привет"


async def test_phrase_passes_slots():
    registry = SkillRegistry()
    skill = Skill("memo")
    skill.command("say", _echo_slots, patterns=[r"^скажи (?P<text>.+)$"],
                  slots={"text": _slot("text")})
    registry.add(skill)

    response = await run_phrase(registry, "скажи привет")

    assert response.meta == {"слоты": {"text": "привет"}}


async def test_missing_slot_asks_instead_of_running():
    """Обвязка не обходит проверку слотов.

    Шаблон без группы `text` даёт пустой слот, и настоящий реестр
    отвечает уточняющим вопросом, а не зовёт обработчик. Обвязка,
    которая вызвала бы его с пустым слотом, проверяла бы не то.
    """
    registry = SkillRegistry()
    skill = Skill("memo")
    skill.command("say", _echo, patterns=[r"^скажи$"],
                  slots={"text": _slot("text")})
    registry.add(skill)

    response = await run_phrase(registry, "скажи")

    assert response.ask_for == "text"
    assert "нужно text" in response.speech


async def test_response_fields_survive():
    registry = SkillRegistry()
    skill = Skill("memo")
    skill.command("say", _full, patterns=[r"^покажи$"])
    registry.add(skill)

    response = await run_phrase(registry, "покажи")

    assert response.speech == "говорю"
    assert response.display == "показываю"
    assert response.follow_up == "ещё что-нибудь?"
    assert response.show_panel is True


async def test_unknown_phrase_raises():
    """Непонятная фраза обязана быть видна, а не съедена.

    Обвязка, вернувшая пустой ответ, прошла бы тест, и промах всплыл бы
    только на живой машине.
    """
    registry = SkillRegistry()
    skill = Skill("memo")
    skill.command("say", _echo, patterns=[r"^скажи (.+)$"])
    registry.add(skill)

    with pytest.raises(PhraseNotMatched) as info:
        await run_phrase(registry, "что-то другое")

    assert "не подошла" in str(info.value)
    assert "memo.say" in str(info.value), "в ошибке перечислены доступные команды"


async def test_empty_phrase_raises():
    registry = SkillRegistry()
    skill = Skill("memo")
    skill.command("say", _echo, patterns=[r"^скажи (.+)$"])
    registry.add(skill)

    with pytest.raises(PhraseNotMatched):
        await run_phrase(registry, "   ")


async def test_phrase_matching_is_case_and_dot_insensitive():
    """Нормализация та же, что в ассистенте: `Скажи.` и `скажи` равны."""
    registry = SkillRegistry()
    skill = Skill("memo")
    skill.command("say", _echo, patterns=[r"^скажи (?P<text>.+)$"],
                  slots={"text": _slot("text")})
    registry.add(skill)

    response = await run_phrase(registry, "Скажи привет.")

    assert response.speech == "услышал привет"


# --- plugin_under_test ------------------------------------------------------


async def test_plugin_runs_through_the_harness(tmp_path):
    async with plugin_under_test(make_folder(tmp_path)) as registry:
        response = await run_phrase(registry, "скажи привет")

        assert response.speech == "услышал привет"


async def test_state_permission_is_granted_and_isolated(tmp_path):
    """Право выдаётся во временный файл, и состояние переживает вызов."""
    async with plugin_under_test(
            make_folder(tmp_path, body=STATE_PLUGIN),
            grant=("state",)) as registry:
        first = await run_phrase(registry, "плюс")
        second = await run_phrase(registry, "плюс")

        assert first.speech == "счётчик 1"
        assert second.speech == "счётчик 2"


async def test_without_grant_state_is_refused(tmp_path):
    """Обвязка не выдаёт права молча.

    Тест, который дал бы себе доступ и забыл, проверил бы не то, что
    собирался.
    """
    async with plugin_under_test(
            make_folder(tmp_path, body=STATE_PLUGIN)) as registry:
        with pytest.raises(Exception) as info:
            await run_phrase(registry, "плюс")

        assert "одобрени" in str(info.value)


async def test_real_approvals_file_untouched(tmp_path):
    """Боевой файл одобрений не трогается — это проверяется на деле."""
    from sinaps.plugins import approvals

    before = approvals.path()
    existed = before.exists()

    async with plugin_under_test(make_folder(tmp_path),
                                 grant=("state",)):
        assert approvals.path() != before

    assert approvals.path() == before
    assert before.exists() == existed, (
        "файл одобрений появился или исчез — обвязка до него дотянулась")


async def test_process_is_closed_after_the_block(tmp_path):
    """Забытый процесс стоит около 30 МБ; набор из сотни тестов встанет."""
    from sinaps.plugins.loader import PluginProcess

    seen: list[PluginProcess] = []
    real_load = PluginProcess.start

    async def spy(self):
        seen.append(self)
        return await real_load(self)

    PluginProcess.start = spy
    try:
        async with plugin_under_test(make_folder(tmp_path)) as registry:
            await run_phrase(registry, "скажи привет")
            assert seen and seen[0].alive
    finally:
        PluginProcess.start = real_load

    assert seen and not seen[0].alive, "процесс плагина остался жить"


async def test_grammar_works_without_grant(tmp_path):
    async with plugin_under_test(make_folder(tmp_path)) as registry:
        with pytest.raises(PhraseNotMatched):
            await run_phrase(registry, "плюс")


# --- make_ctx ---------------------------------------------------------------


def test_make_ctx_defaults():
    ctx = make_ctx("привет")

    assert ctx.text == "привет"
    assert ctx.source == "text"
    assert ctx.user_name == "Капитан"
    assert ctx.history == []


def test_make_ctx_carries_platform():
    platform = FakePlatform()
    ctx = make_ctx("привет", platform=platform)

    assert ctx.platform is platform


def test_make_ctx_history_is_copied():
    source = [{"role": "user", "content": "было"}]
    ctx = make_ctx("привет", history=source)

    ctx.history.append({"role": "user", "content": "новое"})

    assert len(source) == 1, "история передана по ссылке, а не скопирована"


# --- FakePlatform -----------------------------------------------------------


def test_fake_records_calls():
    platform = FakePlatform()

    platform.launch_app("Блокнот")
    platform.minimize_all()

    assert platform.calls == ["launch:Блокнот", "minimize_all"]


def test_fake_volume_changes_for_real():
    """Громкость меняется по-настоящему.

    Навык читает уровень до и после и отвечает «теперь N процентов».
    Заглушка, всегда возвращающая одно и то же, проверяла бы себя.
    """
    platform = FakePlatform(volume_percent=50)

    platform.volume_up(10)
    assert platform.volume_state().percent == 60

    platform.volume_set(30)
    assert platform.volume_state().percent == 30


def test_fake_volume_clamps():
    platform = FakePlatform(volume_percent=95)

    platform.volume_up(20)
    assert platform.volume_state().percent == 100

    platform.volume_down(500)
    assert platform.volume_state().percent == 0


def test_fake_launch_reports_missing_app():
    """Проверка ошибок — тоже проверка кода, а не заглушки."""
    platform = FakePlatform()

    ok, _ = platform.launch_app("Блокнот")
    bad, why = platform.launch_app("нету такого")

    assert ok is True
    assert bad is False
    assert why == "не найдено"


def test_fake_clipboard_round_trip():
    platform = FakePlatform()

    platform.clipboard_set("новый текст")

    assert platform.clipboard_get().text == "новый текст"


def test_fake_status_is_stable():
    first = FakePlatform().status()
    second = FakePlatform().status()

    assert first == second, "состояние системы у заглушки должно быть ровным"


def test_fake_power_action_is_recorded():
    platform = FakePlatform()

    platform.power_action(PowerAction.SHUTDOWN, delay_s=5)

    assert platform.calls == ["power:shutdown:5"]


def test_fake_resolves_process_names():
    platform = FakePlatform()

    assert platform.resolve_process_name("Блокнот") == "notepad"
    assert platform.resolve_process_name("калькулятор") == "calc"
    assert platform.resolve_process_name("неизвестное") == ""


# --- вспомогательные функции для тестов ------------------------------------


async def _echo(ctx, slots):
    return Response(speech="услышал " + str(slots.get("text")))


async def _echo_slots(ctx, slots):
    return Response(speech="услышал", meta={"слоты": dict(slots)})


async def _full(ctx, slots):
    return Response(speech="говорю", display="показываю",
                    follow_up="ещё что-нибудь?", show_panel=True)


def _slot(name: str):
    from sinaps.skills.base import SlotSpec

    return SlotSpec(name=name, type="string", required=True)