"""Уборка простояющих плагинов: связывание в рантайме.

Почему отдельный файл, если `reap_idle` и `stop_all` уже покрыты в
`test_loader.py`: покрыт был **загрузчик**. А не работало **приложение**.
`LoadReport` был локальной переменной в `build_skills_runtime` и терялся
на выходе из функции — оба метода были написаны, оба были зелёные, и ни
один раз не вызваны. Тесты загрузчика врали спокойно, потому что
проверяли не ту часть системы.

Здесь проверяется именно связка: отчёт доезжает до `Runtime`, фоновая
задача создаётся, при остановке всё закрывается.

Модели не грузятся: `Runtime.__new__(Runtime)` собирает объект без
всей остальной машинерии — тот же приём, что в `test_singleton.py`.
"""

from __future__ import annotations

import asyncio
import textwrap
import types

import pytest

from sinaps.plugins import approvals
from sinaps.plugins.loader import LoadReport, load_all
from sinaps.runtime import Runtime
from sinaps.skills.registry import SkillRegistry


WORKING = """\
    from sinaps.plugins.api import Response, Skill

    async def handle(ctx, slots):
        return Response(speech="услышал", display="плагин ответил")

    def register(registry):
        skill = Skill("get")
        skill.command("now", handle, patterns=[r"^погода$"],
                      examples=["погода"], slots={})
        registry.add(skill)
        return skill
"""


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry()


@pytest.fixture(autouse=True)
def isolated_approvals(tmp_path, monkeypatch):
    """Одобрения — во временный каталог, боевой `data/plugins.json` не трогаем."""
    monkeypatch.setattr(approvals, "path", lambda: tmp_path / "plugins.json")


def make_plugin(folder, name="weather"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(textwrap.dedent(f"""\
        name: {name}
        version: 1.0.0
        description: Тестовый плагин
        entry: plugin:register
        permissions:
          - state
        """), encoding="utf-8")
    (folder / "plugin.py").write_text(
        textwrap.dedent(WORKING), encoding="utf-8")
    return folder


def bare_runtime(report: LoadReport | None) -> Runtime:
    """`Runtime` без моделей и микрофона.

    Через `__new__`, а не через конструктор: датакласс требует движки,
    а поднять faster-whisper и Silero ради проверки связи — это ровно то
    расходование памяти, которого мы здесь избегаем. Заглушки повторяют
    приём из `test_singleton.py`.
    """
    class Engine:
        """Прогрев, который ничего не грузит, но вызывается."""

        def warmup(self) -> None:
            pass

    class Listener:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    runtime = Runtime.__new__(Runtime)
    runtime.listener = Listener()
    runtime.trigger = None
    runtime.tts = Engine()
    runtime.stt = Engine()
    runtime.player = types.SimpleNamespace(cancel=lambda: None)
    runtime._reaper = None
    runtime.plugins = report
    return runtime


# --- связывание --------------------------------------------------------------


async def test_reaper_task_is_started(tmp_path, registry):
    """Уборщик поднимается вместе с ассистентом."""
    make_plugin(tmp_path / "a", name="a")
    report = await load_all(tmp_path, registry)
    runtime = bare_runtime(report)

    try:
        await runtime.start()

        assert runtime._reaper is not None, "уборка не запущена"
    finally:
        await runtime.stop()


async def test_stop_closes_plugin_processes(tmp_path, registry):
    """Остановка ассистента закрывает процессы плагинов.

    Раньше это не происходило: процессы доживали только до EOF в трубе,
    то есть задолго до своей смерти. Память при этом держалась всё время
    работы приложения.
    """
    make_plugin(tmp_path / "a", name="a")
    report = await load_all(tmp_path, registry)
    runtime = bare_runtime(report)

    assert all(p.process.alive for p in report.loaded)

    await runtime.stop()

    assert all(not p.process.alive for p in report.loaded), \
        "процессы плагинов остались жить после остановки ассистента"
    assert runtime._reaper is None, "уборка не остановлена"


async def test_double_start_does_not_leak_reaper(tmp_path, registry):
    """Повторный `start()` не плодит вторую уборку.

    Иначе старая задача осталась бы жить, а `self._reaper` указывал бы на
    новую — и старую нечем было бы погасить при остановке.
    """
    make_plugin(tmp_path / "a", name="a")
    report = await load_all(tmp_path, registry)
    runtime = bare_runtime(report)

    try:
        await runtime.start()
        first = runtime._reaper
        await runtime.start()

        assert runtime._reaper is first, "поднялась вторая задача уборки"
    finally:
        await runtime.stop()

    assert first.done(), "первая задача уборки не остановлена"


async def test_no_plugins_means_no_reaper():
    """Без плагинов задача не создаётся: ждать нечего."""
    runtime = bare_runtime(None)

    await runtime.start()

    assert runtime._reaper is None

    await runtime.stop()


async def test_empty_report_means_no_reaper(tmp_path, registry):
    """Отчёт есть, плагинов в нём нет — уборщик всё равно лишний."""
    runtime = bare_runtime(LoadReport())

    await runtime.start()

    assert runtime._reaper is None

    await runtime.stop()


# --- сам ход уборки ----------------------------------------------------------


async def test_reaper_loop_reaps(monkeypatch):
    """Фоновая задача действительно зовёт `reap_idle`."""
    from sinaps import runtime as runtime_module

    calls: list = []

    async def fake_reap(report, idle_s=None):
        calls.append(idle_s)

    monkeypatch.setattr(runtime_module, "reap_idle", fake_reap)
    # Порог меньше секунды, но шаг ограничен снизу — ждём один тик.
    monkeypatch.setattr(runtime_module, "IDLE_TIMEOUT_S", 0.4)

    runtime = bare_runtime(LoadReport())
    task = asyncio.create_task(runtime._reap_idle_forever())
    try:
        await asyncio.sleep(1.3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls, "уборка не вызвана ни разу"


async def test_reaper_survives_broken_reap(monkeypatch):
    """Сбой уборки не должен ронять ассистента."""
    from sinaps import runtime as runtime_module

    async def boom(report, idle_s=None):
        raise RuntimeError("уборка сломалась")

    monkeypatch.setattr(runtime_module, "reap_idle", boom)
    monkeypatch.setattr(runtime_module, "IDLE_TIMEOUT_S", 0.4)

    runtime = bare_runtime(LoadReport())
    task = asyncio.create_task(runtime._reap_idle_forever())
    try:
        await asyncio.sleep(1.3)
        assert not task.done(), "уборщик умер от чужой ошибки"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_reaper_does_not_leak_task(tmp_path, registry):
    """Задача отменяется, а не бросается на произвол ГС.

    Без отмены на выходе светится «Task was destroyed but it is pending».
    """
    make_plugin(tmp_path / "a", name="a")
    report = await load_all(tmp_path, registry)
    runtime = bare_runtime(report)

    await runtime.start()
    task = runtime._reaper

    await runtime.stop()

    assert task.done(), "задача уборки не остановлена при выходе"


# --- проверки-нарушители -----------------------------------------------------
# Убери вызов из `Runtime.start()` или `Runtime.stop()` — тесты упадут.
# Именно такая проверка нужна была проекту: без неё баг прожил два этапа.


async def test_start_still_wires_the_reaper(tmp_path, registry, monkeypatch):
    """`Runtime.start()` обязан поднимать уборку.

    Ловит откат, который не поймал бы ни один тест загрузчика: механизм
    остался бы на месте, просто перестал бы вызываться — как было до
    этого изменения.
    """
    make_plugin(tmp_path / "a", name="a")
    report = await load_all(tmp_path, registry)

    calls: list[str] = []
    original = Runtime._start_plugin_reaper

    def spy(self) -> None:
        calls.append("reaper")
        original(self)

    monkeypatch.setattr(Runtime, "_start_plugin_reaper", spy)

    runtime = bare_runtime(report)
    try:
        await runtime.start()
    finally:
        await runtime.stop()

    assert calls == ["reaper"], "start() больше не поднимает уборку"


# Отдельная проверка «сборщик рантайма передаёт отчёт в Runtime» смотрит не
# на текст файла, а на скомпилированный код. Первая версия искала строку в
# исходнике через `inspect.getsource` и упала не по делу: файл правили прямо
# во время прогона, номера строк сдвинулись, и вернулся чужой кусок файла.
# Проверка, которая может упасть не по той причине, хуже отсутствующей.


def test_build_runtime_passes_the_report():
    """Сборщик обязан передавать отчёт в `Runtime`.

    Потеря этой ссылки — причина, по которой механизм был мёртвым: отчёт
    жил локальной переменной и исчезал вместе с кадром стека. Тесты выше
    подставляют отчёт руками и такой обрыв не заметят, поэтому он ловится
    отдельно.

    Аргументы вызова лежат в `co_consts` кортежем имён. Полная сборка
    рантайма не проверяется: она тянет модели.
    """
    from sinaps.runtime import build_skills_runtime

    kwargs = [c for c in build_skills_runtime.__code__.co_consts
              if isinstance(c, tuple) and "plugins" in c]

    assert kwargs, (
        "сборщик рантайма не передаёт отчёт о плагинах в Runtime — "
        "уборка и закрытие процессов останутся неприделанными")