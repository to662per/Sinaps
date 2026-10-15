# -*- coding: utf-8 -*-
"""Микрофон, подключённый после старта.

Сценарий, который нашёл пользователь: ассистент запустили без
микрофона, микрофон подключили через минуту — и ассистент молчал до
перезапуска.

Проверяется не «микрофон появился», а весь путь: контур поднялся,
событие ушло, интерфейс узнал. Сломаться может любая из трёх
ссылок, и одна зелёная проверка на середине цепочки не скажет ни
о чём.
"""

from __future__ import annotations

import asyncio

import pytest

from sinaps.core.bus import EventBus, EventType
from sinaps.runtime import MIC_WATCH_INTERVAL_S, Runtime


class FakeCapture:
    """Микрофон, который можно включить и выключить."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class FakeListener:
    """Контур прослушивания.

    `start()` открывает устройство — так же, как настоящий
    `ListeningPipeline`. Подделка, которая этого не делает, проверила
    бы не то, что проверяется: «контур поднялся» и «микрофон открыт»
    — разные утверждения.
    """

    def __init__(self, capture) -> None:
        self.capture = capture
        self.started = False

    async def start(self) -> None:
        await self.capture.start()
        self.started = True

    async def stop(self) -> None:
        await self.capture.stop()
        self.started = False


@pytest.fixture
def runtime_without_mic(monkeypatch):
    """Рантайм, собранный без микрофона, как при старте без него."""
    import sinaps.runtime as runtime_module
    from sinaps.core.config import SinapsConfig

    class FakeSTT:
        name = "заглушка"
        device = "cpu"
        _model = None
        min_confidence = 0.5

    built: list[FakeCapture] = []

    def _fake_source(*_a, **_kw):
        capture = FakeCapture()
        built.append(capture)
        return capture

    def _fake_listener(capture, **kwargs):
        return FakeListener(capture)

    monkeypatch.setattr(runtime_module, "ListeningPipeline",
                        _fake_listener)
    import sinaps.audio.source as source_module
    monkeypatch.setattr(source_module, "make_source", _fake_source)

    rt = Runtime(
        config=SinapsConfig(),
        bus=EventBus(),
        session=None,
        metrics=None,
        capture=None,
        player=None,
        stt=FakeSTT(),
        tts=None,
        listener=None,
        conversation=None,
    )
    rt._built_captures = built      # noqa: SLF001
    return rt


def _plug_mic(monkeypatch, present: bool) -> None:
    """Сказать рантайму, есть микрофон или нет."""
    import sinaps.audio.devices as devices

    monkeypatch.setattr(devices, "has_input_device", lambda: present)


# --- главный сценарий --------------------------------------------------------


@pytest.mark.asyncio
async def test_microphone_plugged_after_start_is_found(runtime_without_mic,
                                                       monkeypatch):
    """Подключённый микрофон поднимает контур прослушивания.

    Именно этот порядок и описывал пользователь: запустили без
    микрофона, подключили, ассистент продолжил молчать.
    """
    _plug_mic(monkeypatch, False)
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"

    assert await runtime.attach_microphone() is False, \
        "без микрофона контур подниматься не должен"

    _plug_mic(monkeypatch, True)

    assert await runtime.attach_microphone() is True
    assert runtime.listener is not None, "контур не поднялся"
    assert runtime.capture is not None
    assert runtime.listener.started


@pytest.mark.asyncio
async def test_listener_actually_starts(runtime_without_mic, monkeypatch):
    """Контур не просто создан, а запущен."""
    _plug_mic(monkeypatch, True)
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"

    await runtime.attach_microphone()

    assert runtime.listener.started, "микрофон открыт, но контур не читает"
    assert runtime._built_captures[0].started, "устройство не открыто"


@pytest.mark.asyncio
async def test_capture_stopped_when_listener_fails(runtime_without_mic,
                                                  monkeypatch):
    """Если контур не поднялся, микрофон закрывается.

    Иначе осталось бы устройство, занятое ассистентом, который им не
    пользуется: следующая программа получит «устройство занято», а
    человек — объяснение, которого нет.
    """
    _plug_mic(monkeypatch, True)
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"

    import sinaps.runtime as runtime_module

    def _boom(**_kwargs):
        return _BrokenListener()

    monkeypatch.setattr(runtime_module, "ListeningPipeline", _boom)

    assert await runtime.attach_microphone() is False

    assert runtime.listener is None, "сломанный контур остался в поле"
    assert runtime._built_captures[0].stopped, "микрофон остался открытым"


class _BrokenListener:
    async def start(self) -> None:
        raise RuntimeError("устройство занято")


# --- без повторов ------------------------------------------------------------


@pytest.mark.asyncio
async def test_attached_twice_does_not_double_listen(runtime_without_mic,
                                                    monkeypatch):
    """Второй вызов не поднимает второй контур.

    Две задачи прослушивания звучали бы одновременно и забирали бы
    звук друг у друга — ассистент слышал бы только себя.
    """
    _plug_mic(monkeypatch, True)
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"

    assert await runtime.attach_microphone() is True
    first = runtime.listener
    assert await runtime.attach_microphone() is False
    assert runtime.listener is first


@pytest.mark.asyncio
async def test_no_watcher_when_microphone_present(runtime_without_mic,
                                                  monkeypatch):
    """При микрофоне на старте слежение не поднимается."""
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"
    _plug_mic(monkeypatch, True)

    listener = FakeListener(FakeCapture())
    runtime.listener = listener
    runtime.capture = listener.capture

    runtime._start_mic_watcher()

    assert runtime._mic_watcher is None, (
        "слежение поднялось, хотя микрофон уже есть")


# --- слежение в фоне ---------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_waits_and_attaches(runtime_without_mic, monkeypatch):
    """Фоновая задача дожидается микрофона и поднимает контур.

    Проверяется именно задача, а не прямой вызов: человек не зовёт
    `attach_microphone`, он подключает наушники.
    """
    _plug_mic(monkeypatch, False)
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"
    monkeypatch.setattr("sinaps.runtime.MIC_WATCH_INTERVAL_S", 0.01)

    runtime._start_mic_watcher()

    await asyncio.sleep(0.05)
    assert runtime.listener is None, "контур поднялся без микрофона"

    _plug_mic(monkeypatch, True)
    await asyncio.sleep(0.15)

    assert runtime.listener is not None, (
        "микрофон подключили, а контур не поднялся")


@pytest.mark.asyncio
async def test_watcher_stops_after_success(runtime_without_mic, monkeypatch):
    """После успеха слежение прекращается.

    Постоянно опрашивать Portaudio ради уже работающего микрофона —
    это трата, которую человек заметит по тому, что ассистент стал
    чуть тяжелее.
    """
    _plug_mic(monkeypatch, False)
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"
    monkeypatch.setattr("sinaps.runtime.MIC_WATCH_INTERVAL_S", 0.01)

    runtime._start_mic_watcher()
    await asyncio.sleep(0.02)
    _plug_mic(monkeypatch, True)
    await asyncio.sleep(0.1)

    assert runtime._mic_watcher.done(), "слежение не завершилось"


@pytest.mark.asyncio
async def test_watcher_survives_broken_device(runtime_without_mic,
                                              monkeypatch):
    """Устройство, которое не открывается, не роняет ассистента.

    Реальный случай: карта появилась в списке, но занять её нельзя.
    Без этого слежение падало бы, и следующая попытка была бы уже
    только при перезапуске.
    """
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"
    _plug_mic(monkeypatch, True)

    calls = {"n": 0}
    import sinaps.runtime as runtime_module

    def _source(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("звуковая карта занята")
        return FakeCapture()

    import sinaps.audio.source as source_module
    monkeypatch.setattr(source_module, "make_source", _source)
    monkeypatch.setattr("sinaps.runtime.MIC_WATCH_INTERVAL_S", 0.01)

    runtime._start_mic_watcher()
    await asyncio.sleep(0.12)

    assert calls["n"] > 1, "после отказа слежение остановилось"
    assert runtime.listener is not None, "вторая попытка не удалась"


@pytest.mark.asyncio
async def test_watcher_cancelled_on_stop(runtime_without_mic, monkeypatch):
    """Остановка гасит слежение."""
    _plug_mic(monkeypatch, False)
    runtime = runtime_without_mic
    monkeypatch.setattr("sinaps.runtime.MIC_WATCH_INTERVAL_S", 0.01)

    class _Player:
        def cancel(self) -> None:
            pass

    runtime.player = _Player()
    runtime.plugins = None
    runtime._start_mic_watcher()
    watcher = runtime._mic_watcher

    await runtime.stop()

    assert watcher.done(), "задача слежения жива после остановки"
    assert runtime._mic_watcher is None


def test_watch_interval_is_sane():
    """Интервал проверки — не тик и не минута.

    Секунда означает, что человек успевает увидеть отклик, а список
    устройств не перечисляется впритык.
    """
    assert 0.2 <= MIC_WATCH_INTERVAL_S <= 5.0


# --- видимость для человека --------------------------------------------------


def test_label_says_microphone_is_absent(runtime_without_mic):
    """Без микрофона подпись прямо говорит об этом."""
    assert "не используется" in runtime_without_mic.capture_label()


@pytest.mark.asyncio
async def test_label_changes_after_attach(runtime_without_mic, monkeypatch):
    """После подключения подпись перестаёт говорить «текстовый режим».

    Иначе в трее осталось бы «не используется», и человек решил бы,
    что микрофон так и не подхватился.
    """
    _plug_mic(monkeypatch, True)
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"

    await runtime.attach_microphone()

    assert "не используется" not in runtime.capture_label()


@pytest.mark.asyncio
async def test_event_announced(runtime_without_mic, monkeypatch):
    """О подключении уходит событие.

    Без него интерфейс не узнает, что ассистент ожил, и будет
    показывать «работает без голоса» при живом микрофоне.
    """
    seen: list[EventType] = []

    async def _watch(event):
        seen.append(event.type)

    runtime_without_mic.bus.on("microphone_attached")(_watch)
    _plug_mic(monkeypatch, True)
    runtime = runtime_without_mic
    runtime.config.wake.mode = "none"

    await runtime.attach_microphone()
    await asyncio.sleep(0.01)

    assert EventType.MICROPHONE_ATTACHED in seen


def test_event_type_exists():
    """Событие объявлено в шине, а не придумано на месте."""
    assert EventType("microphone_attached") is EventType.MICROPHONE_ATTACHED