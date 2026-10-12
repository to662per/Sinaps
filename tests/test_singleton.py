"""Защита от второго экземпляра.

Контекст. На машине пользователя работали две копии ассистента: из
`run_sinaps.bat` и из `run_sinaps_silent.bat`. Обе держали микрофон,
и симптом был «микрофон не работает»:

    Фраза завершена: 0.98 с
    Фраза завершена: 0.98 с
    'Субтитры субтитров Н.Новикова' (conf=0.06)

Фразы одинаковой длины и одинаковые выдумки — это не ВАД и не микрофон.
Два процесса делят один входной поток PortAudio.

Тесты проверяют сам механизм, без запуска ассистента.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from sinaps.core.singleton import PID_FILE, SingleInstance, _process_alive


# --- захват ---------------------------------------------------------------


def test_first_instance_acquires():
    """Первый должен получить право — иначе ассистент не запустится вовсе."""
    guard = SingleInstance(name="Sinaps-test-acquire")
    try:
        assert guard.acquire() is None, "первый экземпляр обязан захватить"
    finally:
        guard.release()


def test_second_instance_is_refused():
    """Второй обязан получить отказ: иначе микрофон делится на две части."""
    first = SingleInstance(name="Sinaps-test-refuse")
    assert first.acquire() is None
    try:
        second = SingleInstance(name="Sinaps-test-refuse")
        busy = second.acquire()
        assert busy is not None, "вторая копия не должна проходить"
    finally:
        first.release()


def test_after_release_another_can_start():
    """Освобождение работает — иначе после /quit запуститься нельзя."""
    first = SingleInstance(name="Sinaps-test-release")
    assert first.acquire() is None
    first.release()

    second = SingleInstance(name="Sinaps-test-release")
    try:
        assert second.acquire() is None, "после освобождения место должно освободиться"
    finally:
        second.release()


def test_different_names_do_not_conflict():
    """Разные экземпляры защиты не должны мешать друг другу."""
    first = SingleInstance(name="Sinaps-test-name-a")
    assert first.acquire() is None
    try:
        other = SingleInstance(name="Sinaps-test-name-b")
        try:
            assert other.acquire() is None, "разные имена — разные блокировки"
        finally:
            other.release()
    finally:
        first.release()


def test_release_is_idempotent():
    """Двойной вызов release() не должен ничего ломать."""
    guard = SingleInstance(name="Sinaps-test-idempotent")
    assert guard.acquire() is None
    guard.release()
    guard.release()          # второй раз — уже нечего закрывать


def test_release_without_acquire_is_safe():
    """Освобождение без захвата — тоже не должно падать."""
    SingleInstance(name="Sinaps-test-unowned").release()


# --- PID ------------------------------------------------------------------


def test_second_sees_pid_of_first():
    """Сообщение должно называть процесс — иначе непонятно, кого убивать."""
    first = SingleInstance(name="Sinaps-test-pid")
    assert first.acquire() is None
    try:
        second = SingleInstance(name="Sinaps-test-pid")
        busy = second.acquire()
        assert busy is not None
        assert busy.pid == os.getpid(), "второй должен видеть PID первого"
    finally:
        first.release()


def test_read_pid_returns_none_without_file(monkeypatch, tmp_path):
    monkeypatch.setattr("sinaps.core.singleton.PID_FILE", tmp_path / "нет.txt")
    assert SingleInstance._read_pid() is None


def test_read_pid_ignores_garbage(monkeypatch, tmp_path):
    """Испорченный файл не должен ронять запуск."""
    bad = tmp_path / "pid.txt"
    bad.write_text("не число", encoding="utf-8")
    monkeypatch.setattr("sinaps.core.singleton.PID_FILE", bad)
    assert SingleInstance._read_pid() is None


def test_stale_pid_file_is_removed(monkeypatch, tmp_path):
    """Файл после аварии не должен блокировать запуск навсегда."""
    stale = tmp_path / "sinaps.pid"
    stale.write_text("999999", encoding="utf-8")     # процесса не существует
    monkeypatch.setattr("sinaps.core.singleton.PID_FILE", stale)

    from sinaps.core.singleton import stop_stale_pid_file

    stop_stale_pid_file()
    assert not stale.exists(), "устаревший файл должен убираться"


def test_live_pid_file_is_kept(monkeypatch, tmp_path):
    """Живой процесс трогать нельзя."""
    live = tmp_path / "sinaps.pid"
    live.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr("sinaps.core.singleton.PID_FILE", live)

    from sinaps.core.singleton import stop_stale_pid_file

    stop_stale_pid_file()
    assert live.exists(), "файл живого процесса должен остаться"


# --- проверка живости -----------------------------------------------------


def test_own_process_is_alive():
    assert _process_alive(os.getpid()) is True


@pytest.mark.parametrize("pid", [0, -1, 999999])
def test_dead_pids(pid):
    assert _process_alive(pid) is False


# --- порядок запуска: модели раньше микрофона -----------------------------
#
# Контекст. Микрофон открывался ДО загрузки моделей. Пока грузятся
# Silero TTS и faster-whisper, главный поток держит GIL минутами, а
# аудиоколбэк PortAudio живёт в отдельном C-потоке и без GIL работать
# не может: буфер переполняется, кадры выбрасываются. В логе это видно
# как «Portaudio: input overflow» сразу после «Микрофон запущен».


@pytest.mark.asyncio
async def test_microphone_opens_after_models_are_warm():
    """Регрессия: микрофон не должен открываться под нагрузкой моделей."""
    from sinaps.runtime import Runtime

    events: list[str] = []

    class SlowEngine:
        """Прогрев, который занимает время и помечает факт вызова."""

        def __init__(self, name: str) -> None:
            self.name = name

        def warmup(self) -> None:
            events.append(f"warm:{self.name}")

    class Listener:
        async def start(self) -> None:
            events.append("mic:start")

        async def stop(self) -> None:
            pass

    runtime = Runtime.__new__(Runtime)     # без полной сборки графа
    runtime.tts = SlowEngine("tts")
    runtime.stt = SlowEngine("stt")
    runtime.listener = Listener()
    runtime.trigger = None

    await runtime.start()

    assert events == ["warm:tts", "warm:stt", "mic:start"], (
        "микрофон обязан открываться последним, иначе кадры теряются")


@pytest.mark.asyncio
async def test_microphone_opens_even_if_warmup_fails():
    """Сломанный прогрев не должен оставить систему без микрофона.

    Иначе «модель не загрузилась» превратится в «ассистент мёртв»:
    голос был бы потерян из-за второй, неосновной проблемы.
    """
    from sinaps.runtime import Runtime

    events: list[str] = []

    class BrokenEngine:
        name = "tts"

        def warmup(self) -> None:
            raise RuntimeError("модель сломана")

    class WorkingEngine:
        name = "stt"

        def warmup(self) -> None:
            events.append("warm:stt")

    class Listener:
        async def start(self) -> None:
            events.append("mic:start")

        async def stop(self) -> None:
            pass

    runtime = Runtime.__new__(Runtime)
    runtime.tts = BrokenEngine()
    runtime.stt = WorkingEngine()
    runtime.listener = Listener()
    runtime.trigger = None

    await runtime.start()

    assert "mic:start" in events, "микрофон должен открыться, даже если TTS сломан"


@pytest.mark.asyncio
async def test_without_microphone_warmup_stays_in_background():
    """Текстовый режим не должен ждать прогрев — там GIL не мешает."""
    from sinaps.runtime import Runtime

    events: list[str] = []

    class SlowEngine:
        def __init__(self, name: str) -> None:
            self.name = name

        def warmup(self) -> None:
            events.append(f"warm:{self.name}")

    runtime = Runtime.__new__(Runtime)
    runtime.tts = SlowEngine("tts")
    runtime.stt = SlowEngine("stt")
    runtime.listener = None
    runtime.trigger = None

    await runtime.start()      # не должен висеть на прогреве
    assert events == [], "прогрев должен идти в фоне"

    await asyncio.sleep(0.05)
    assert "warm:tts" in events


def test_message_explains_the_cause():
    """Сообщение должно называть причину, а не просто «занято»."""
    from sinaps.core.singleton import list_instance_note

    text = list_instance_note(1234)
    assert "уже запущен" in text
    assert "1234" in text
    assert "микрофон" in text.lower(), "надо объяснить, почему это важно"


def test_message_without_pid_still_readable():
    from sinaps.core.singleton import list_instance_note

    text = list_instance_note(None)
    assert "уже запущен" in text
    assert "None" not in text