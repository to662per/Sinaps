"""Активация голосового ввода горячей клавишей или кнопкой мыши.

Почему это отдельный модул��, а не строчка в конфиге
---------------------------------------------------
Win32 `RegisterHotKey` умеет только клавиатуру. Боковые кнопки мыши
(`mouse4`/`mouse5`) Windows отдаёт окну отдельными сообщениями
`WM_XBUTTONDOWN`, и чтобы их увидеть, нужен low-level hook
(`WH_MOUSE_LL`). `pynput` закрывает оба случая одним API, поэтому в
настройках клавиши и кнопки мыши выглядят одинаково:

    activate: mouse5              # клик — открыть окно прослушивания
    push_to_talk: mouse4          # удержание — говори, пока держишь

Потоки
------
`pynput` вызывает обработчики из своего потока ОС, а event loop живёт в
главном. Любой вызов в асинхронный код обязан идти через
`loop.call_soon_threadsafe`: `asyncio.create_task()` вне своего цикла
роняет процесс. Именно на этом уже обжигался консольный ввод — см.
`_on_console_text` в `__main__.py`.

Проверяемость
-------------
`pynput` импортируется лениво, а обработчики событий вынесены в обычные
методы `_handle_key` / `_handle_button`, которым не нужен ни реальный
`pynput`, ни event loop: достаточно подставить свой `dispatcher`. Поэтому
вся логика покрыта тестами без реальной мыши.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

log = logging.getLogger(__name__)


#: Приведение имён клавиш к канону: pynpt отдаёт `Key.ctrl_l`, конфиг
#: пишет `ctrl`. Без этого `ctrl+space` никогда не совпал бы сам с собой.
_KEY_ALIASES: dict[str, str] = {
    "control": "ctrl", "ctl": "ctrl", "lctrl": "ctrl", "rctrl": "ctrl",
    "ctrl_l": "ctrl", "ctrl_r": "ctrl", "ctrl": "ctrl",
    "shift_l": "shift", "shift_r": "shift", "shift": "shift",
    "alt_l": "alt", "alt_r": "alt", "alt": "alt", "option": "alt",
    "alt_gr": "altgr", "altgr": "altgr",
    "cmd": "win", "cmd_l": "win", "cmd_r": "win", "command": "win",
    "super": "win", "meta": "win",
    "win_l": "win", "win_r": "win", "win": "win",
    "space": "space",
}

#: Имена кнопок мыши в терминах pynput.
_MOUSE_ALIASES: dict[str, str] = {
    "mouse1": "left", "mouse2": "right", "mouse3": "middle",
    "mouse4": "x1", "mouse5": "x2",
    "left": "left", "right": "right", "middle": "middle",
    "x1": "x1", "x2": "x2",
    "back": "x1", "forward": "x2",
    "назад": "x1", "вперёд": "x2", "вперед": "x2",
}

#: Порядок вывода в сообщениях: модификаторы вперёд.
_KEY_ORDER = ("ctrl", "alt", "shift", "win", "altgr")

#: Только модификатор — не активация: нажатие `ctrl` не должно будить ассистента.
_MODIFIERS = frozenset(_KEY_ORDER)

_MOUSE_RU = {
    "x1": "боковая «назад» (mouse4)",
    "x2": "боковая «вперёд» (mouse5)",
    "middle": "средняя кнопка",
    "left": "левая кнопка",
    "right": "правая кнопка",
}

#: Окно прослушивания при удержании: закрываемся только по отпусканию.
PUSH_TO_TALK_TIMEOUT = float("inf")


@dataclass(frozen=True)
class Trigger:
    """Одно нажимаемое устройство: сочетание клавиш или кнопка мыши.

    `keys` заполняется и для кнопки мыши — если написано `ctrl+mouse5`,
    кнопка сработает только при зажатом `ctrl`.
    """

    kind: str                                  # "key" | "mouse"
    keys: frozenset[str] = frozenset()         # канонические имена клавиш
    button: str = ""                           # имя кнопки мыши
    raw: str = ""                              # как написано в конфиге

    def matches_keys(self, pressed: frozenset[str]) -> bool:
        """Все требуемые клавиши-модификаторы нажаты.

        Пустое множество — это «модификаторы не нужны», а не «не задано».
        Иначе кнопка мыши без модификатора (`mouse5`) не срабатывала бы
        никогда: множество было пустым и отбрасывалось как ложное.
        """
        return self.keys <= pressed

    def describe(self) -> str:
        """Человекочитаемое имя для консоли и логов."""
        if self.kind == "mouse":
            button = _MOUSE_RU.get(self.button, self.button)
            return f"{'+'.join(self._key_parts())}+{button}" if self.keys else button
        return "+".join(self._key_parts()) or self.raw

    def _key_parts(self) -> list[str]:
        ordered = [k for k in _KEY_ORDER if k in self.keys]
        return ordered + sorted(self.keys - set(ordered))


def _canonical(token: str) -> str:
    return _KEY_ALIASES.get(token, token)


def parse_trigger(spec: str, *, allow_modifiers_only: bool = False) -> Trigger | None:
    """Разбирает строку настройки в `Trigger`.

    Поддерживает: `ctrl+space`, `f9`, `mouse5`, `back`, `ctrl+mouse5`,
    а также отключение через `none`.

    `allow_modifiers_only` нужен для push-to-talk: `ctrl+shift` там законно
    и стандартно, потому что требует УДЕРЖАНИЯ двух клавиш. Для обычной
    активации такое сочетание запрещено — его нажал бы кто угодно, и
    ассистент просыпался бы на ровном месте.
    """
    raw = (spec or "").strip()
    if not raw or raw.lower() in {"none", "нет", "off", "-"}:
        return None

    lowered = raw.lower()

    if lowered in _MOUSE_ALIASES:
        return Trigger(kind="mouse", button=_MOUSE_ALIASES[lowered], raw=raw)

    parts = [p.strip().lower() for p in lowered.split("+") if p.strip()]
    if not parts:
        return None

    # Сочетание с кнопкой мыши: допускаем ровно один модификатор.
    mouse_parts = [p for p in parts if p in _MOUSE_ALIASES]
    if mouse_parts:
        if len(mouse_parts) > 1:
            log.warning("Непонятная настройка активации: %r", raw)
            return None
        rest = [p for p in parts if p is not mouse_parts[0]]
        if len(rest) > 1 or (rest and _canonical(rest[0]) not in _MODIFIERS):
            log.warning("Непонятная настройка активации: %r", raw)
            return None
        return Trigger(
            kind="mouse",
            button=_MOUSE_ALIASES[mouse_parts[0]],
            keys=frozenset(_canonical(r) for r in rest),
            raw=raw,
        )

    keys = frozenset(_canonical(p) for p in parts)
    if keys <= _MODIFIERS and not allow_modifiers_only:
        log.warning(
            "Настройка активации %r состоит только из модификаторов — "
            "такое сочетание нажал бы каждый, кто работает с клавиатурой. "
            "Для удержания (push_to_talk) оно допустимо.", raw)
        return None

    return Trigger(kind="key", keys=keys, raw=raw)


def describe_specs(activate: "Trigger | Iterable[Trigger] | None",
                   hold: Trigger | None) -> str:
    """Строка для стартового экрана: чем и как активировать."""
    parts = [f"{t.describe()} — открыть окно" for t in _as_triggers(activate)]
    if hold is not None:
        parts.append(f"{hold.describe()} — удерживать (говори)")
    return "; ".join(parts) if parts else "не задана"


def _as_triggers(
    value: "Trigger | Iterable[Trigger] | None",
) -> tuple[Trigger, ...]:
    """Принимает один триггер, список или None — приводит к кортежу."""
    if value is None:
        return ()
    if isinstance(value, Trigger):
        return (value,)
    return tuple(t for t in value if t is not None)


class ActivationTrigger:
    """Слушает клавиатуру и/или мышь и сообщает об активации.

    `activate_triggers` — список: активирует любое из сочетаний. Нужен
    именно список, а не один триггер, потому что кнопка мыши и клавиша
    должны работать одновременно: если софт мыши перехватил боковую
    кнопку, ассистент остаётся управляем с клавиатуры, и наоборот.

    Обработчики вызываются в event loop и могут быть корутинами.
    Контракт подписи жёсткий:

        on_activate(source: str, timeout_s: float | None) -> None
        on_deactivate(source: str) -> None

    `timeout_s` равен `PUSH_TO_TALK_TIMEOUT` при удержании и `None` при
    обычной активации (тогда окно берётся из `listen_timeout_s`).

    `dispatcher` можно подменить — этим пользуются тесты, чтобы не
    зависеть от реального устройства ввода.
    """

    def __init__(
        self,
        *,
        activate: "Trigger | Iterable[Trigger] | None" = None,
        hold: Trigger | None = None,
        on_activate: Callable[..., object],
        on_deactivate: Callable[..., object],
        loop: asyncio.AbstractEventLoop | None = None,
        debounce_s: float = 0.35,
        dispatcher: Callable[[Callable[..., None], tuple], None] | None = None,
    ) -> None:
        self.activate_triggers = _as_triggers(activate)
        self.hold = hold
        self.on_activate = on_activate
        self.on_deactivate = on_deactivate
        self.loop = loop
        self.debounce_s = debounce_s

        self._dispatcher = dispatcher or self._dispatch_to_loop
        self._lock = threading.Lock()

        # Состояние нажатых клавиш и кнопок — обновляется из потока ОС.
        self._keys: set[str] = set()
        self._buttons: set[str] = set()
        self._hold_active = False
        self._last_fire = 0.0
        # Триггеры, которые прямо сейчас нажаты — для срабатывания по переходу.
        self._fired: set[int] = set()

        self._key_listener = None
        self._mouse_listener = None
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False

    @property
    def activate(self) -> Trigger | None:
        """Первый триггер активации — для совместимости и диагностики."""
        return self.activate_triggers[0] if self.activate_triggers else None

    def _all_triggers(self) -> tuple[Trigger, ...]:
        return tuple(self.activate_triggers) + ((self.hold,) if self.hold else ())

    # --- запуск ------------------------------------------------------------

    def start(self) -> str | None:
        """Запускает перехват. Возвращает текст ошибки или `None`."""
        self._stopped = False

        every = self._all_triggers()
        needs_keyboard = any(t.kind == "key" or t.keys for t in every)
        needs_mouse = any(t.kind == "mouse" for t in every)

        problems: list[str] = []
        if needs_keyboard:
            problem = self._start_keyboard()
            if problem:
                problems.append(problem)
        if needs_mouse:
            problem = self._start_mouse()
            if problem:
                problems.append(problem)

        if problems:
            return "; ".join(problems)
        return None

    def _start_keyboard(self) -> str | None:
        try:
            from pynput import keyboard
        except Exception as exc:  # pragma: no cover - зависит от окружения
            return f"не удалось подключить клавиатуру: {exc}"

        try:
            listener = keyboard.Listener(
                on_press=self._on_key_press, on_release=self._on_key_release)
            listener.daemon = True
            listener.start()
        except Exception as exc:
            return (f"клавиатура недоступна ({exc}). Запусти от администратора "
                    "или проверь, не перехватывает ли клавиши другая программа")
        self._key_listener = listener
        return None

    def _start_mouse(self) -> str | None:
        try:
            from pynput import mouse
        except Exception as exc:  # pragma: no cover - зависит от окружения
            return f"не удалось подключить мышь: {exc}"

        try:
            listener = mouse.Listener(on_click=self._on_click)
            listener.daemon = True
            listener.start()
        except Exception as exc:
            return (f"мышь недоступна ({exc}). Чаще всего боковые кнопки "
                    "перехватывает софт мыши — Logitech G Hub, Razer Synapse, "
                    "X-Mouse; проверь, не назначено ли им «назад/вперёд»")
        self._mouse_listener = listener
        return None

    def stop(self) -> None:
        self._stopped = True

        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

        for listener in (self._key_listener, self._mouse_listener):
            if listener is None:
                continue
            try:
                listener.stop()
            except Exception:
                log.debug("Не удалось остановить перехват", exc_info=True)
        self._key_listener = None
        self._mouse_listener = None

    def describe(self) -> str:
        return describe_specs(self.activate_triggers, self.hold)

    # --- мост в event loop -------------------------------------------------

    def _dispatch_to_loop(self, callback: Callable[..., None], args: tuple) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._submit, callback, args)
        except RuntimeError:  # цикл уже остановлен
            log.debug("Event loop закрыт — активация проигнорирована")

    def _submit(self, callback: Callable[..., None], args: tuple) -> None:
        """Оборачивает вызов в задачу.

        `call_soon_threadsafe` не умеет запускать корутины: он вызовет
        `_run(...)`, получит от неё корутину и молча выбросит — без ошибки,
        без предупреждения в логе приложения и, главное, без эффекта.
        Активация просто перестаёт работать, и причина неочевидна.
        Раньше это ловилось только предупреждением Python в stderr.

        Флаг `_stopped` закрывает окно между постановкой в очередь и
        запуском задачи: без него нажатие кнопки в момент выхода из
        программы успевало бы открыть окно прослушивания уже после
        остановки.
        """
        if self._stopped:
            return
        task = asyncio.ensure_future(self._run(callback, args))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, callback: Callable[..., None], args: tuple) -> None:
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("Ошибка обработки активации")

    # --- события pynput (поток ОС) -----------------------------------------

    def _on_key_press(self, key) -> None:  # noqa: ANN001
        self._handle_key(pressed=True, names=_key_names(key))

    def _on_key_release(self, key) -> None:  # noqa: ANN001
        self._handle_key(pressed=False, names=_key_names(key))

    def _on_click(self, x, y, button, pressed) -> None:  # noqa: ANN001
        self._handle_button(getattr(button, "name", ""), pressed)

    # --- логика (пригодна для прямого вызова в тестах) ---------------------

    def _handle_key(self, *, pressed: bool, names: Iterable[str]) -> None:
        # Канонизируем здесь, а не только в `_key_names`: состояние
        # `_keys` сравнивается с каноническими именами из конфига, значит
        # любой вход должен приводиться к тому же виду. Иначе событие,
        # пришедшее не из pynput, тихо не сработает.
        canonical = {_canonical(n) for n in names if n}
        with self._lock:
            if pressed:
                self._keys |= canonical
            else:
                self._keys -= canonical
            self._reconsider()

    def _handle_button(self, name: str, pressed: bool) -> None:
        if not name:
            return
        with self._lock:
            if pressed:
                self._buttons.add(name)
            else:
                self._buttons.discard(name)
            self._reconsider()

    def _matches(self, trigger: Trigger, keys: frozenset[str]) -> bool:
        """Нажат ли триггер прямо сейчас."""
        if not trigger.matches_keys(keys):
            return False
        if trigger.kind == "mouse":
            return trigger.button in self._buttons
        return True

    def _reconsider(self) -> None:
        """Решает, открыть ли окно. Вызывается под блокировкой.

        Порядок важен: удержание имеет приоритет, но пока оно не начато,
        обычная активация продолжает работать.

        Обычная активация срабатывает по ПЕРЕХОДУ — когда триггер стал
        нажат, а не пока он нажат. Иначе два неприятных эффекта: удержание
        кнопки переоткрывало бы окно каждые debounce_s секунд, а список
        триггеров просматривался бы по порядку — и первая нажатая кнопка
        мыши «съедала» бы сочетание клавиш, которое нажали следом.
        """
        keys = frozenset(self._keys)

        hold = self.hold
        if hold is not None:
            if self._matches(hold, keys):
                if not self._hold_active:
                    self._hold_active = True
                    self._emit_activate(hold, hold=True)
                return
            if self._hold_active:
                self._hold_active = False
                self._dispatcher(self.on_deactivate, ("hold_released",))
                return
            # Удержание настроено, но не нажато — обычная активация жива.

        matched = [t for t in self.activate_triggers if self._matches(t, keys)]
        matched_ids = {id(t) for t in matched}
        # Порядок activate_triggers сохраняем: сообщение должно быть
        # предсказуемым, если совпало несколько триггеров сразу.
        newly = [t for t in matched if id(t) not in self._fired]
        self._fired = matched_ids

        if not newly:
            return

        now = time.monotonic()
        if now - self._last_fire < self.debounce_s:
            return
        self._last_fire = now
        self._emit_activate(newly[0], hold=False)

    def _emit_activate(self, target: Trigger, *, hold: bool) -> None:
        timeout = PUSH_TO_TALK_TIMEOUT if hold else None
        self._dispatcher(self.on_activate, (target.raw, timeout))


def _key_names(key) -> frozenset[str]:  # noqa: ANN001
    """Канонические имена одного события клавиатуры.

    `Key` даёт `.name` (`ctrl_l`, `space`, `f9`), `KeyCode` — `.char`.
    Пустой результат — событие нам неинтересно, его игнорируем: иначе
    мусорные коды вроде DeadKey попадали бы в набор нажатых.
    """
    names: set[str] = set()

    name = getattr(key, "name", None)
    if name:
        names.add(_canonical(str(name).lower()))

    char = getattr(key, "char", None)
    if char:
        names.add(str(char).lower())

    return frozenset(n for n in names if n)