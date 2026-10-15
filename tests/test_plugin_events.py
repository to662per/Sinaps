# -*- coding: utf-8 -*-
"""События жизненного цикла плагинов.

Трей, а в Beta 1.1 — окно «Плагины» должны знать, что плагин поднялся
или уснул. Иначе состояние «спит» пришлось бы угадывать опросом процессов,
а это ровно то, чего мы избавлялись.

Здесь проверяется не «событие объявлено в шине» — это проверяется само
собой, — а что оно действительно **приходит** в тот момент, когда
плагин поднялся, уснул или не смог проснуться.
"""

from __future__ import annotations

import asyncio

import pytest

from sinaps.core.bus import EventBus, EventType
from sinaps.plugins import approvals
from sinaps.plugins.loader import load_all, reap_idle
from sinaps.skills.registry import SkillRegistry


PLUGIN = '''\
from sinaps.plugins.api import Response, Skill

async def handle(ctx, slots):
    return Response(speech="услышал")

def register(registry):
    skill = Skill("get")
    skill.command("now", handle, patterns=[r"^погода$"],
                  examples=["погода"], slots={})
    registry.add(skill)
    return skill
'''

BROKEN_ON_WAKE = """\
from sinaps.plugins.api import Response, Skill

import os
import sys

# Портится при ПОВТОРНОМ запуске процесса: первый старт проходит,
# второй — нет. Именно этот случай должен ловить PLUGIN_WAKE_FAILED.
# Состояние берётся из окружения: переменная в модуле при перезапуске
# обнулилась бы и поломка не наступила.
if os.environ.get("SINAPS_TEST_BREAK_ON_RESTART") == "1":
    print("плагин сломался при подъёме", file=sys.stderr)
    sys.exit(3)

async def handle(ctx, slots):
    return Response(speech="услышал")

def register(registry):
    skill = Skill("get")
    skill.command("now", handle, patterns=[r"^погода$"],
                  examples=["погода"], slots={})
    registry.add(skill)
    return skill
"""

MANIFEST = """\
name: {name}
version: 1.0.0
description: Тестовый плагин
entry: plugin:register
permissions:
  - state
"""


def make_plugin(folder, name="weather", body=PLUGIN):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(
        MANIFEST.format(name=name), encoding="utf-8")
    (folder / "plugin.py").write_text(body, encoding="utf-8")
    return folder


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry()


@pytest.fixture(autouse=True)
def isolated_approvals(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "path", lambda: tmp_path / "plugins.json")


class Recorder:
    """Собирает события жизненного цикла."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.events: list[tuple[EventType, dict]] = []

    def watch(self, *types: EventType) -> "Recorder":
        async def handler(event) -> None:  # noqa: ANN001
            if not types or event.type in types:
                self.events.append((event.type, dict(event.payload)))

        self.bus.on(EventType.PLUGIN_LOADED)(handler)
        self.bus.on(EventType.PLUGIN_UNLOADED)(handler)
        self.bus.on(EventType.PLUGIN_WAKE_FAILED)(handler)
        return self

    def of(self, event_type: EventType) -> list[dict]:
        return [p for t, p in self.events if t is event_type]

    def names(self, event_type: EventType) -> list[str]:
        return [p.get("plugin") for p in self.of(event_type)]


async def test_loaded_is_emitted(tmp_path, registry):
    """Подъём плагина объявляется событием."""
    make_plugin(tmp_path / "a", name="a")
    rec = Recorder().watch()

    report = await load_all(tmp_path, registry, bus=rec.bus)
    try:
        assert rec.names(EventType.PLUGIN_LOADED) == ["a"]
        assert rec.of(EventType.PLUGIN_LOADED)[0]["skills"] == 1
    finally:
        await report.stop_all()


async def test_unloaded_on_idle_says_idle(tmp_path, registry):
    """Засыпание по простоям отличается от остановки нарочно.

    `reason` — единственное, по чему подписчик понимает: плагин сам
    освободил память или его погасили при выходе.
    """
    make_plugin(tmp_path / "a", name="a")
    rec = Recorder().watch()

    report = await load_all(tmp_path, registry, bus=rec.bus)
    try:
        await reap_idle(report, 0)

        payloads = rec.of(EventType.PLUGIN_UNLOADED)
        assert [p["plugin"] for p in payloads] == ["a"]
        assert payloads[0]["reason"] == "idle", payloads[0]
    finally:
        await report.stop_all()


async def test_unloaded_on_stop_says_stop(tmp_path, registry):
    """При остановке ассистента причина — «stop»."""
    make_plugin(tmp_path / "a", name="a")
    rec = Recorder().watch()

    report = await load_all(tmp_path, registry, bus=rec.bus)
    await report.stop_all(reason="stop")

    payloads = rec.of(EventType.PLUGIN_UNLOADED)
    assert [p["reason"] for p in payloads] == ["stop"], payloads


async def test_second_stop_emits_nothing(tmp_path, registry):
    """Повторный stop не порождает событие.

    Иначе счётчик засыпаний в окне разъезжался бы с реальностью —
    особенно при остановке, когда гасят всё подряд.
    """
    make_plugin(tmp_path / "a", name="a")
    rec = Recorder().watch()

    report = await load_all(tmp_path, registry, bus=rec.bus)
    await report.stop_all(reason="stop")
    await report.stop_all(reason="stop")

    assert len(rec.of(EventType.PLUGIN_UNLOADED)) == 1, (
        "повторная остановка породила лишнее событие")


async def test_reload_sleep_and_wake_emits_both(tmp_path, registry):
    """Цикл «поднялся → уснул → снова поднялся» виден целиком.

    Именно этот цикл окно «Плагины» будет показывать как «активен» и
    «спит». Если бы терялась хоть одна половина, индикатор врёт.
    """
    from tests.test_loader import FakeCtx, find

    make_plugin(tmp_path / "a", name="a")
    rec = Recorder().watch()

    report = await load_all(tmp_path, registry, bus=rec.bus)
    try:
        await find(registry, "a.get.now").handler(FakeCtx(), {})
        await reap_idle(report, 0)
        await find(registry, "a.get.now").handler(FakeCtx(), {})

        loaded = rec.names(EventType.PLUGIN_LOADED)
        unloaded = rec.of(EventType.PLUGIN_UNLOADED)
        assert loaded == ["a", "a"], loaded
        assert len(unloaded) == 1 and unloaded[0]["reason"] == "idle"
    finally:
        await report.stop_all()


async def test_wake_failure_is_reported(tmp_path, registry, monkeypatch):
    """Неудачное пробуждение после сна — отдельное событие.

    Первая загрузка и повторная после сна — разные вещи для человека:
    в первом случае сбой при старте, во втором посреди работы, и
    ждать придётся не перезапуска.
    """
    make_plugin(tmp_path / "a", name="a", body=BROKEN_ON_WAKE)
    rec = Recorder().watch()

    report = await load_all(tmp_path, registry, bus=rec.bus)
    try:
        plugin = report.loaded[0]
        assert plugin.process.alive, "первая загрузка не удалась"

        # Гасим. Следующий старт упадёт — процесс запускается с
        # переменной окружения, читаемой кодом плагина при импорте.
        await plugin.stop(reason="idle")
        monkeypatch.setenv("SINAPS_TEST_BREAK_ON_RESTART", "1")
        with pytest.raises(Exception):
            await plugin.process.start()

        failures = rec.of(EventType.PLUGIN_WAKE_FAILED)
        assert failures, "неудачное пробуждение не объявлено"
        assert failures[0]["plugin"] == "a"
        assert failures[0]["error"], "в событии нет причины"
    finally:
        await report.stop_all()


async def test_first_load_failure_is_not_wake_failure(tmp_path, registry):
    """Сбой при ПЕРВОЙ загрузке — не «не смог проснуться»."""
    body = "import нет_такого_модуля  # noqa\n"
    make_plugin(tmp_path / "a", name="a", body=body)
    rec = Recorder().watch()

    report = await load_all(tmp_path, registry, bus=rec.bus)
    try:
        assert report.failed, "сломанный плагин не попал в отчёт"
        assert not rec.of(EventType.PLUGIN_WAKE_FAILED), (
            "сбой при первой загрузке выдан за неудачное пробуждение")
    finally:
        await report.stop_all()


async def test_events_survive_broken_subscriber(tmp_path, registry):
    """Упавший подписчик не должен ронять плагин.

    Событие — рассказ о том, что произошло, а не часть того, что
    происходит. Окно с ошибкой в коде не имеет права останавливать
    загрузку плагинов.
    """
    make_plugin(tmp_path / "a", name="a")
    bus = EventBus()

    async def broken(event) -> None:  # noqa: ANN001
        raise RuntimeError("подписчик упал")

    bus.on(EventType.PLUGIN_LOADED)(broken)

    report = await load_all(tmp_path, registry, bus=bus)
    try:
        assert report.loaded, "упавший подписчик сорвал загрузку"
        assert report.loaded[0].process.alive
    finally:
        await report.stop_all()


async def test_no_bus_still_works(tmp_path, registry):
    """Без шины всё работает: события просто никому не уходят."""
    make_plugin(tmp_path / "a", name="a")

    report = await load_all(tmp_path, registry)
    try:
        assert report.loaded[0].process.alive
        await report.stop_all()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"работа без шины сломалась: {exc}")


async def test_runtime_passes_bus_to_loader(tmp_path, monkeypatch):
    """Рантайм обязан отдать шину загрузчику.

    Проверяется на скомпилированном коде, а не на тексте файла: правило
    проекта — «замер, а не предположение», а текст подвержен правкам,
    из-за которых проверка падает не по той причине.
    """
    from sinaps.runtime import build_runtime

    kwargs = [c for c in build_runtime.__code__.co_consts
              if isinstance(c, tuple) and "bus" in c]

    assert kwargs, "рантайм не передаёт шину загрузчику плагинов"