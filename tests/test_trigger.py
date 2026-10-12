"""Тесты активации: клавиши и кнопки мыши.

Проверяются без реальных устройств ввода. Обработчики `_handle_key` и
`_handle_button` не требуют ни `pynput`, ни event loop — достаточно
подставить свой `dispatcher`, который зовёт колбэбы напрямую.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from sinaps.core.config import SinapsConfig
from sinaps.runtime import build_trigger
from sinaps.ui.trigger import (
    ActivationTrigger,
    Trigger,
    describe_specs,
    parse_trigger,
)


# --- разбор настроек -------------------------------------------------------


@pytest.mark.parametrize("spec,button", [
    ("mouse4", "x1"),
    ("mouse5", "x2"),
    ("MOUSE5", "x2"),
    ("mouse3", "middle"),
    ("middle", "middle"),
    ("back", "x1"),
    ("forward", "x2"),
    ("назад", "x1"),
    ("вперёд", "x2"),
])
def test_mouse_buttons_are_recognised(spec, button):
    """Боковые кнопки мыши — основной способ активации."""
    trigger = parse_trigger(spec)
    assert trigger is not None
    assert trigger.kind == "mouse"
    assert trigger.button == button


@pytest.mark.parametrize("spec,keys", [
    ("ctrl+space", {"ctrl", "space"}),
    ("CTRL+Space", {"ctrl", "space"}),
    ("f9", {"f9"}),
    ("ctrl+shift+s", {"ctrl", "shift", "s"}),
])
def test_keyboard_combos_are_recognised(spec, keys):
    trigger = parse_trigger(spec)
    assert trigger is not None
    assert trigger.kind == "key"
    assert set(trigger.keys) == keys


def test_modifier_names_are_canonical():
    """`control` и `ctrl` — одно и то же, иначе сочетание не сработает."""
    assert parse_trigger("control+space").keys == parse_trigger("ctrl+space").keys
    assert parse_trigger("cmd+space").keys == frozenset({"win", "space"})
    assert parse_trigger("alt+f4").keys == frozenset({"alt", "f4"})


def test_mouse_with_modifier():
    """`ctrl+mouse5` — кнопка с зажатым ctrl."""
    trigger = parse_trigger("ctrl+mouse5")
    assert trigger is not None
    assert trigger.kind == "mouse"
    assert trigger.button == "x2"
    assert trigger.keys == frozenset({"ctrl"})


@pytest.mark.parametrize("spec", [
    "", "   ", "none", "нет", "-", "off",
    "ctrl", "shift", "ctrl+shift",  # только модификаторы — сработает у всех
    "ctrl+mouse4+mouse5",           # две кнопки мыши — неоднозначно
    "ctrl+middle+space",            # кнопка мыши с двумя клавишами
])
def test_nonsense_is_rejected(spec):
    """Плохая настройка не должна превращаться в «будит всех»."""
    assert parse_trigger(spec) is None


def test_trigger_describe_is_readable():
    assert parse_trigger("mouse5").describe() == "боковая «вперёд» (mouse5)"
    assert parse_trigger("ctrl+mouse5").describe() == "ctrl+боковая «вперёд» (mouse5)"
    assert parse_trigger("ctrl+space").describe() == "ctrl+space"
    assert "alt" in parse_trigger("ctrl+alt+delete").describe()


def test_describe_specs_mentions_both():
    text = describe_specs(parse_trigger("mouse5"), parse_trigger("mouse4"))
    assert "открыть окно" in text
    assert "удерживать" in text


# --- механика --------------------------------------------------------------


class FakeKey:
    """Имитация pynput.keyboard.Key."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.char = None


class FakeChar:
    """Имитация pynput.keyboard.KeyCode."""

    def __init__(self, char: str) -> None:
        self.char = char
        self.name = None


class Recorder:
    """Колбэк-приёмник: запоминает вызовы вместо запуска event loop."""

    def __init__(self) -> None:
        self.activations: list[tuple[str, float | None]] = []
        self.deactivations: list[str] = []

    def activate(self, source: str, timeout_s: float | None) -> None:
        self.activations.append((source, timeout_s))

    def deactivate(self, source: str) -> None:
        self.deactivations.append(source)

    def dispatch(self, callback, args) -> None:
        callback(*args)


def make_trigger(**kwargs) -> tuple[ActivationTrigger, Recorder]:
    rec = Recorder()
    kwargs.setdefault("debounce_s", 0.0)
    trigger = ActivationTrigger(
        on_activate=rec.activate,
        on_deactivate=rec.deactivate,
        dispatcher=rec.dispatch,
        **kwargs,
    )
    return trigger, rec


# --- активация мышью -------------------------------------------------------


def test_mouse5_click_opens_listening_window():
    trigger, rec = make_trigger(activate=parse_trigger("mouse5"))
    trigger._handle_button("x2", True)

    assert len(rec.activations) == 1
    source, timeout = rec.activations[0]
    assert source == "mouse5"
    # Обычная активация: окно ограничено listen_timeout_s,
    # timeout=None означает «взять из конфига».
    assert timeout is None


def test_mouse5_release_does_not_reactivate():
    """Кнопка не должна срабатывать повторно на отпускании."""
    trigger, rec = make_trigger(activate=parse_trigger("mouse5"))
    trigger._handle_button("x2", True)
    trigger._handle_button("x2", False)

    assert len(rec.activations) == 1


def test_other_mouse_buttons_are_ignored():
    trigger, rec = make_trigger(activate=parse_trigger("mouse5"))
    for button in ("left", "right", "middle", "x1"):
        trigger._handle_button(button, True)

    assert rec.activations == [], "назначена только mouse5"


def test_mouse_with_modifier_requires_modifier():
    trigger, rec = make_trigger(activate=parse_trigger("ctrl+mouse5"))

    trigger._handle_button("x2", True)
    assert rec.activations == [], "без ctrl не должно сработать"

    trigger._handle_key(pressed=True, names={"ctrl"})
    assert len(rec.activations) == 1


# --- push-to-talk ----------------------------------------------------------


def test_push_to_talk_opens_on_press_and_closes_on_release():
    """Удержание: окно открыто ровно пока кнопка зажата."""
    trigger, rec = make_trigger(hold=parse_trigger("mouse4"))

    trigger._handle_button("x1", True)
    assert len(rec.activations) == 1
    source, timeout = rec.activations[0]
    assert source == "mouse4"
    # Бесконечное окно: закрыть должен только пользователь.
    assert timeout == float("inf")

    assert rec.deactivations == []

    trigger._handle_button("x1", False)
    assert rec.deactivations == ["hold_released"]


def test_push_to_talk_is_not_deactivated_twice():
    trigger, rec = make_trigger(hold=parse_trigger("mouse4"))

    trigger._handle_button("x1", True)
    trigger._handle_button("x1", False)
    trigger._handle_button("x1", False)

    assert rec.deactivations == ["hold_released"], "повторного отпускания быть не должно"


def test_push_to_talk_hold_does_not_matter_for_mouse():
    """Наклон мыши или движение не должны закрывать окно."""
    trigger, rec = make_trigger(hold=parse_trigger("mouse4"))

    trigger._handle_button("x1", True)
    trigger._handle_button("left", True)   # нажали левую, не отпустив боковую
    assert rec.deactivations == []

    trigger._handle_button("x1", False)
    assert rec.deactivations == ["hold_released"]


def test_keyboard_push_to_talk():
    trigger, rec = make_trigger(
        hold=parse_trigger("ctrl+shift", allow_modifiers_only=True))

    trigger._handle_key(pressed=True, names={"ctrl_l"})
    assert rec.activations == []

    trigger._handle_key(pressed=True, names={"shift_l"})
    assert len(rec.activations) == 1

    trigger._handle_key(pressed=False, names={"shift_l"})
    assert rec.deactivations == ["hold_released"]


def test_modifier_only_is_rejected_for_activation():
    """Для обычной активации `ctrl+shift` запрещён: нажмёт кто угодно."""
    assert parse_trigger("ctrl+shift") is None
    # ...но для удержания — законный и стандартный вариант.
    assert parse_trigger("ctrl+shift", allow_modifiers_only=True) is not None


def test_push_to_talk_suppresses_clicks_while_held():
    """Пока говоришь, случайный клик не должен запускать новую команду.

    Иначе во время фразы можно случайно открыть программу или выключить
    что-то — ровно то, чего push-to-talk и нужен, чтобы избежать.
    """
    trigger, rec = make_trigger(
        activate=parse_trigger("mouse5"), hold=parse_trigger("mouse4"))

    trigger._handle_button("x1", True)          # начали говорить
    trigger._handle_button("x2", True)          # случайный клик
    trigger._handle_button("x2", False)

    assert [s for s, _ in rec.activations] == ["mouse4"], (
        "удержание должно подавлять обычную активацию")

    # А после отпускания клик снова работает.
    trigger._handle_button("x1", False)
    trigger._handle_button("x2", True)

    assert [s for s, _ in rec.activations] == ["mouse4", "mouse5"]


# --- клавиатура ------------------------------------------------------------


def test_hotkey_opens_listening_window():
    trigger, rec = make_trigger(activate=parse_trigger("ctrl+space"))

    trigger._handle_key(pressed=True, names={"ctrl_l"})
    assert rec.activations == []

    trigger._handle_key(pressed=True, names={"space"})
    assert len(rec.activations) == 1


def test_hotkey_repeats_are_debounced():
    """Автоповтор клавиатуры не должен открывать окно десять раз."""
    rec = Recorder()
    trigger = ActivationTrigger(
        activate=parse_trigger("f9"),
        on_activate=rec.activate,
        on_deactivate=rec.deactivate,
        dispatcher=rec.dispatch,
        debounce_s=0.5,
    )

    for _ in range(20):
        trigger._handle_key(pressed=True, names={"f9"})
        trigger._handle_key(pressed=False, names={"f9"})

    assert len(rec.activations) == 1


def test_mouse_and_keyboard_work_together():
    """Мышь и клавиатура настроены одновременно — обе работают."""
    trigger, rec = make_trigger(
        activate=[parse_trigger("mouse5"), parse_trigger("ctrl+space")],
        hold=parse_trigger("mouse4"))

    trigger._handle_button("x2", True)
    trigger._handle_button("x2", False)

    trigger._handle_key(pressed=True, names={"ctrl_l"})
    trigger._handle_key(pressed=True, names={"space"})
    trigger._handle_key(pressed=False, names={"space"})
    trigger._handle_key(pressed=False, names={"ctrl_l"})

    sources = [s for s, _ in rec.activations]
    assert "mouse5" in sources and "ctrl+space" in sources


def test_single_click_does_not_reactivate_other_trigger():
    """Дебаунс общий: мышь и клавиша не должны сработать обе сразу."""
    trigger, rec = make_trigger(
        activate=[parse_trigger("mouse5"), parse_trigger("ctrl+space")],
        debounce_s=10.0)

    trigger._handle_button("x2", True)
    trigger._handle_key(pressed=True, names={"ctrl_l", "space"})

    assert len(rec.activations) == 1, "один жест — одно окно"


def test_duplicate_triggers_are_not_doubled():
    """Если мышь и клавиша прописаны одинаково, срабатывание одно."""
    cfg = SinapsConfig()
    cfg.wake.mouse_button = "ctrl+space"   # абсурд, но не должно ломать
    trigger = build_trigger(cfg, object())

    assert len(trigger.activate_triggers) == 1


# --- устойчивость ----------------------------------------------------------


def test_unknown_events_are_ignored():
    """Мусорные коды клавиатуры не должны ломать состояние."""
    trigger, rec = make_trigger(activate=parse_trigger("ctrl+space"))

    trigger._handle_key(pressed=True, names=set())   # DeadKey и подобное
    trigger._handle_button("", True)

    assert rec.activations == []


def test_stuck_modifier_does_not_block():
    """Залипший ctrl не должен помешать нажать ctrl+space."""
    trigger, rec = make_trigger(activate=parse_trigger("ctrl+space"))

    trigger._handle_key(pressed=True, names={"ctrl_l", "alt_l"})
    trigger._handle_key(pressed=True, names={"space"})

    assert len(rec.activations) == 1


def test_stop_without_start_is_safe():
    trigger, _ = make_trigger(activate=parse_trigger("mouse5"))
    trigger.stop()  # не должно быть исключения


def test_no_triggers_configured():
    """Пустая настройка не должна поднимать перехват вообще."""
    trigger, rec = make_trigger()
    assert trigger.describe() == "не задана"


# --- настоящий event loop --------------------------------------------------
#
# Тесты выше подставляют синхронный `dispatcher` и потому не проверяют
# главное: доходит ли вызов до корутины в event loop. Регрессия ниже.


@pytest.mark.asyncio
async def test_callbacks_reach_the_event_loop():
    """Регрессия: `call_soon_threadsafe` не умеет запускать корутины.

    Ошибка выглядела безобидно: вместо вызова в цикл попадала
    корутина, которую event loop молча выбрасывал. Ни исключения, ни
    записи в лог приложения — просто активация ничего не делала.
    """
    rec = Recorder()

    async def on_activate(source, timeout_s):
        rec.activations.append((source, timeout_s))

    async def on_deactivate(source):
        rec.deactivations.append(source)

    trigger = ActivationTrigger(
        activate=parse_trigger("mouse5"),
        hold=parse_trigger("mouse4"),
        on_activate=on_activate,
        on_deactivate=on_deactivate,
        loop=asyncio.get_running_loop(),
        debounce_s=0.0,
    )

    trigger._handle_button("x2", True)
    trigger._handle_button("x2", False)
    trigger._handle_button("x1", True)
    trigger._handle_button("x1", False)

    # Даём планировщику выполнить задачи.
    for _ in range(10):
        await asyncio.sleep(0)

    assert [s for s, _ in rec.activations] == ["mouse5", "mouse4"], (
        "корутины должны выполниться в event loop")
    assert rec.deactivations == ["hold_released"]


@pytest.mark.asyncio
async def test_broken_callback_does_not_kill_the_loop():
    """Ошибка в обработчике не должна ронять ни цикл, ни последующие нажатия."""
    rec = Recorder()
    calls = []

    async def broken(source, timeout_s):
        calls.append(source)
        raise RuntimeError("взрыв")

    async def on_deactivate(source):
        rec.deactivations.append(source)

    trigger = ActivationTrigger(
        activate=parse_trigger("mouse5"),
        on_activate=broken,
        on_deactivate=on_deactivate,
        loop=asyncio.get_running_loop(),
        debounce_s=0.0,
    )

    # Настоящие переходы: нажатие-отпускание, нажатие-отпускание.
    trigger._handle_button("x2", True)
    trigger._handle_button("x2", False)
    trigger._handle_button("x2", True)
    for _ in range(10):
        await asyncio.sleep(0)

    assert len(calls) == 2, "после исключения перехват обязан продолжать работать"


@pytest.mark.asyncio
async def test_events_from_another_thread_reach_the_loop():
    """Поток ОС (pynput) -> event loop: ровно тот путь, что в бою."""
    rec = Recorder()

    async def on_activate(source, timeout_s):
        rec.activations.append((source, timeout_s))

    trigger = ActivationTrigger(
        activate=parse_trigger("mouse5"),
        on_activate=on_activate,
        on_deactivate=rec.deactivate,
        loop=asyncio.get_running_loop(),
        debounce_s=0.0,
    )

    def os_thread():
        """Имитация потока, из которого pynpt зовёт обработчик."""
        trigger._handle_button("x2", True)
        trigger._handle_button("x2", False)

    thread = threading.Thread(target=os_thread)
    thread.start()
    thread.join()

    for _ in range(10):
        await asyncio.sleep(0)

    assert [s for s, _ in rec.activations] == ["mouse5"]


@pytest.mark.asyncio
async def test_stop_cancels_pending_tasks():
    rec = Recorder()
    trigger = ActivationTrigger(
        activate=parse_trigger("mouse5"),
        on_activate=rec.activate,
        on_deactivate=rec.deactivate,
        loop=asyncio.get_running_loop(),
    )

    trigger._handle_button("x2", True)
    trigger.stop()

    for _ in range(10):
        await asyncio.sleep(0)

    assert rec.activations == [], "остановленный триггер не должен срабатывать"


# --- сборка из конфига -----------------------------------------------------


def test_build_trigger_from_config(monkeypatch):
    """Конфиг из коробки даёт мышь, а не клавиатуру."""
    from sinaps.core.config import SinapsConfig
    from sinaps.runtime import build_trigger

    class FakeListener:
        def __init__(self):
            self.calls = []

        async def activate(self, source, timeout_s=None):
            self.calls.append(("activate", source, timeout_s))

        async def deactivate(self, reason):
            self.calls.append(("deactivate", reason))

    cfg = SinapsConfig()
    listener = FakeListener()

    trigger = build_trigger(cfg, listener)
    assert trigger is not None
    assert trigger.activate is not None and trigger.activate.kind == "mouse"
    assert trigger.activate.button == "x2"
    assert trigger.hold is not None and trigger.hold.button == "x1"

    text = trigger.describe()
    assert "вперёд" in text and "назад" in text


def test_build_trigger_wires_both_mouse_and_keyboard():
    """Кнопка мыши и клавиша включаются вместе, а не по очереди."""
    trigger = build_trigger(SinapsConfig(), object())

    kinds = {t.kind for t in trigger.activate_triggers}
    assert kinds == {"mouse", "key"}, (
        "и мышь, и клавиатура должны быть подключены сразу")

    text = trigger.describe()
    assert "вперёд" in text, "мышь в сообщении"
    assert "ctrl+space" in text, "клавиатура в сообщении"


def test_holding_button_does_not_reopen_window():
    """Регрессия: удержание переоткрывало окно каждые debounce_s секунд.

    Кнопка мыши без модификатора совпадает при любом состоянии клавиатуры,
    поэтому проверка «триггер нажат» без учёта перехода срабатывала снова
    и снова, пока кнопка зажата.
    """
    rec = Recorder()
    trigger = ActivationTrigger(
        activate=parse_trigger("mouse5"),
        on_activate=rec.activate,
        on_deactivate=rec.deactivate,
        dispatcher=rec.dispatch,
        debounce_s=0.0,          # даже без дебаунса лишнего быть не должно
    )

    trigger._handle_button("x2", True)
    for _ in range(10):
        # Клавиатура шевелится, кнопка мыши всё ещё зажата.
        trigger._handle_key(pressed=True, names={"a"})
        trigger._handle_key(pressed=False, names={"a"})

    assert len(rec.activations) == 1, (
        "удержание кнопки не должно переоткрывать окно прослушивания")


def test_keyboard_works_while_mouse_button_held():
    """Регрессия: нажатая кнопка мыши «съедала» сочетание клавиш.

    Триггеры проверялись по порядку, и первый совпавший всегда был
    мышиный — до клавиатуры очередь не доходила.
    """
    trigger, rec = make_trigger(
        activate=[parse_trigger("mouse5"), parse_trigger("ctrl+space")])

    trigger._handle_button("x2", True)          # держим мышь
    trigger._handle_key(pressed=True, names={"ctrl_l"})
    trigger._handle_key(pressed=True, names={"space"})

    assert [s for s, _ in rec.activations] == ["mouse5", "ctrl+space"], (
        "сочетание клавиш должно сработать, даже если кнопка мыши зажата")


def test_duplicate_triggers_are_not_doubled():
    """Если мышь и клавиша прописаны одинаково, срабатывание одно."""
    cfg = SinapsConfig()
    cfg.wake.mouse_button = "ctrl+space"   # абсурд, но не должно ломать
    trigger = build_trigger(cfg, object())

    assert len(trigger.activate_triggers) == 1


# --- команда hotkeys ------------------------------------------------------
#
# Регрессия: обработчик активации объявлял один аргумент, а вызывался
# с двумя — `(откуда, таймаут)`. Команда падала с TypeError и показывала
# «не поймана» даже когда кнопка только что сработала.


def _press_all(trigger: ActivationTrigger, items) -> None:
    """Нажимает триггеры так, как это сделал бы пользователь."""
    for item in items:
        if item.kind == "mouse":
            trigger._handle_button(item.button, True)
        else:
            for name in item.keys:
                trigger._handle_key(pressed=True, names={name})


def test_hotkeys_command_reports_caught_buttons(monkeypatch, capsys):
    """Команда проверки не должна падать и обязана узнавать свои кнопки."""
    import argparse
    import asyncio

    from sinaps import __main__ as main

    def fake_start(self):
        every = list(self.activate_triggers) + ([self.hold] if self.hold else ())
        _press_all(self, every)
        return None

    monkeypatch.setattr(ActivationTrigger, "start", fake_start)

    args = argparse.Namespace(seconds=0.05)
    code = asyncio.run(main.cmd_hotkeys(args))

    out = capsys.readouterr().out
    assert "Traceback" not in out, f"команда упала:\n{out}"
    assert "TypeError" not in out
    assert code == 0, f"все кнопки пойманы, ожидался код 0:\n{out}"
    assert out.count("[OK]") == 3, f"ожидались три найденные кнопки:\n{out}"


def test_hotkeys_command_explains_when_nothing_is_caught(monkeypatch, capsys):
    """Если не поймано ничего, подсказка должна быть про сессию, а не про мышь."""
    import argparse
    import asyncio

    from sinaps import __main__ as main

    monkeypatch.setattr(ActivationTrigger, "start", lambda self: None)

    args = argparse.Namespace(seconds=0.05)
    code = asyncio.run(main.cmd_hotkeys(args))

    out = capsys.readouterr().out
    assert code == 1
    assert "перехват не работает" in out.lower()
    assert "администратора" in out


def test_hotkeys_command_reports_missing_mouse_button(monkeypatch, capsys):
    """Пропала именно мышь — подсказка должна быть про софт мыши."""
    import argparse
    import asyncio

    from sinaps import __main__ as main

    def fake_start(self):
        """Нажимаем только клавиатуру: мышь как будто съедена софтом."""
        _press_all(self, [t for t in self.activate_triggers if t.kind == "key"])
        return None

    monkeypatch.setattr(ActivationTrigger, "start", fake_start)

    args = argparse.Namespace(seconds=0.05)
    code = asyncio.run(main.cmd_hotkeys(args))

    out = capsys.readouterr().out
    assert code == 1
    assert "Razer" in out or "G Hub" in out, f"нужен совет про софт мыши:\n{out}"
    assert "Дополнительные параметры" in out, "подсказка про настройки Windows полезна"


def test_build_trigger_disabled_by_mode():
    cfg = SinapsConfig()
    cfg.wake.mode = "none"

    assert build_trigger(cfg, object()) is None


def test_fallback_to_keyboard_when_mouse_unset():
    """Если кнопка мыши не задана, работает клавиатура."""
    from sinaps.core.config import SinapsConfig
    from sinaps.runtime import build_trigger

    cfg = SinapsConfig()
    cfg.wake.mouse_button = ""
    cfg.wake.push_to_talk = ""

    trigger = build_trigger(cfg, object())
    assert trigger is not None
    assert trigger.activate.kind == "key"
    assert trigger.hold is None


def test_keyboard_and_mouse_both_configured():
    """Кнопка мыши основная, клавиатура — запасная."""
    trigger = build_trigger(SinapsConfig(), object())
    # Основным выбирается мышь, но клавиша тоже остаётся в запасе
    # иначе пришлось бы перезапускать ассистент, чтобы переключиться.
    assert trigger.activate.kind == "mouse"