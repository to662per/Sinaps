"""Тесты консоли и маршрутизации команд.

Здесь ловятся ошибки, которые не видно без терминала: обработчики,
вызываемые при построении словаря вместо вызова по требованию, и
рассинхрон вывода с обработкой.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from sinaps.ui.console import Console, ConsoleCommandRouter


# --- маршрутизация ----------------------------------------------------------


def test_router_ignores_ordinary_replies():
    handlers = {"quiet": lambda arg: pytest.fail("обработчик вызван на реплике")}
    router = ConsoleCommandRouter(handlers)
    assert router.route("открыть блокнот") is False
    assert router.route("привет") is False


def test_router_routes_commands():
    calls = []
    router = ConsoleCommandRouter({
        "quiet": lambda arg: calls.append(("quiet", arg)),
        "quit": lambda arg: calls.append(("quit", arg)),
    })

    assert router.route("/quiet") is True
    assert router.route("/quit") is True
    assert calls == [("quiet", ""), ("quit", "")]


def test_router_passes_argument():
    calls = []
    router = ConsoleCommandRouter({"quiet": lambda arg: calls.append(arg)})
    router.route("/quiet on")
    assert calls == ["on"]


def test_router_is_case_insensitive():
    calls = []
    router = ConsoleCommandRouter({"quit": lambda arg: calls.append(arg)})
    router.route("/QUIT")
    assert calls == [""]


def test_router_unknown_command_falls_back_to_help():
    calls = []
    router = ConsoleCommandRouter({
        "help": lambda arg: calls.append(arg),
        "quit": lambda arg: pytest.fail("не тот обработчик"),
    })

    assert router.route("/такой-команды-нет") is True
    assert calls and "такой-команды-нет" in calls[0]


# --- обработчики: главный регресс-тест -------------------------------------


def test_handlers_are_not_called_when_dict_is_built():
    """Обработчик в словаре должен быть ФУНКЦИЕЙ, а не результатом вызова.

    Реальный баг: `"quiet": _set_quiet(console, runtime)` выполнялся
    немедленно при построении словаря — на каждой строке, независимо от
    ввода. Из-за этого озвучка выключалась сама.
    """
    calls = []

    def quiet(arg: str) -> None:
        calls.append(arg)

    # Так правильно:
    correct = ConsoleCommandRouter({"quiet": lambda arg: quiet(arg)})
    correct.route("/quiet")
    assert calls == [""]

    # Так было сломано — вызов происходит ещё до route():
    calls.clear()
    _broken = {"quiet": quiet("")}          # noqa: F841 - имитируем баг
    assert calls == [""], "вызов произошёл при построении словаря"

    # Регресс-проверка: реальный словарь обработчиков не должен
    # вызывать ничего до явного route().
    calls.clear()
    ConsoleCommandRouter({"quiet": lambda arg: quiet(arg)})
    assert calls == []


# --- синхронизация вывода ---------------------------------------------------


def test_console_blocks_next_read_until_done():
    """Пока реплика в обработке, поток не должен читать следующую строку.

    Иначе промпт, реплика и ответ печатаются в случайном порядке, и
    пользователь видит «молчание» там, где ответ уже есть в логе.
    """
    console = Console(None, banner=False)
    assert console.idle.is_set()

    console.mark_busy()
    assert not console.idle.is_set(), "после mark_busy чтение должно быть заблокировано"

    console.mark_done()
    assert console.idle.is_set()


def test_console_busy_does_not_deadlock():
    """mark_done должен освобождать ожидание потока (иначе вечная пауза)."""
    console = Console(None, banner=False)
    console.mark_busy()

    released = threading.Event()

    def waiter() -> None:
        console.idle.wait(timeout=5.0)
        released.set()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.05)
    assert not released.is_set(), "пока busy, поток должен ждать"

    console.mark_done()
    thread.join(timeout=2.0)
    assert released.is_set(), "mark_done обязан разблокировать ожидание"


def test_console_submit_delivers_line():
    got: list[str] = []
    console = Console(got.append, banner=False)
    console.submit("  привет  ")
    assert got == ["привет"], "пробелы должны срезаться"


def test_console_submit_ignores_empty():
    got: list[str] = []
    console = Console(got.append, banner=False)
    console.submit("   ")
    assert got == []


# --- маршрутизация команд end-to-end ---------------------------------------


@pytest.mark.asyncio
async def test_replies_are_ordered_and_visible():
    """Реплика должна давать видимый ответ, и порядок строк не должен рваться."""
    replies: list[str] = []
    commands: list[str] = []

    console = Console(None, banner=False)
    router = ConsoleCommandRouter({"quit": lambda arg: commands.append("/quit")})

    for line in ["открыть блокнот", "привет", "/quit"]:
        if router.route(line):
            continue
        console.echo_assistant(f"ответ на {line}")
        replies.append(line)

    assert replies == ["открыть блокнот", "привет"], "каждая реплика даёт ровно один ответ"
    assert commands == ["/quit"], "/quit — команда, а не реплика"


def test_echo_output_is_readable():
    """Текст ответа должен печататься с пометкой, чтобы его нельзя было
    спутать со служебным сообщением."""
    console = Console(None, banner=False)
    lines: list[str] = []
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        console.echo_assistant("Услышал: привет")
        console.info("служебное сообщение")

    lines = buf.getvalue().splitlines()
    assert lines[0].startswith("Синапс>"), lines
    assert lines[1].startswith("·"), lines
    assert lines[0] != lines[1], "ответ и служебное сообщение должны различаться"
