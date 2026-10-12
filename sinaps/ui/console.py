"""Текстовая консоль — рабочий интерфейс этапа 1.

Это не «заглушка на потом», а основной способ общения с ассистентом на первом
этапе и полноценная альтернатива голосу навсегда: если микрофон не слышно
или шумно, можно просто написать команду.

Читает stdin в отдельном потоке (блокирующий вызов нельзя делать в event loop)
и публикует реплику через колбэк.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

BANNER = r"""
   ┌───────────────────────────────────────────────┐
   │  Sinaps — локальный голосовой ассистент        │
   │  Введите текст и нажмите Enter.               │
   │  Команды: /help  /devices  /metrics  /quit    │
   └───────────────────────────────────────────────┘
"""

HELP = """
Команды консоли:
  /help              эта справка
  /devices          список микрофонов и динамиков
  /config           текущая конфигурация
  /metrics          статистика задержек
  /history          история диалогов
  /reload           перечитать config.yaml
  /quiet on|off     включить/выключить озвучку
  /quit             выход

Просто пишите обычным текстом — это реплика ассистенту.
"""


class Console:
    """Неблокирующий ввод из stdin + вывод в консоль.

    Ключевая деталь — `busy`. Без неё поток чтения забирает следующую строку
    сразу, не дожидаясь ответа ассистента. Тогда промпт, реплика и ответ
    печатаются в случайном порядке, и пользователь видит «молчание» там,
    где ответ уже есть в логе.

    Поэтому чтение синхронизировано: пока предыдущая реплика обрабатывается,
    новую строку не читаем. Это ровно то поведение, которое нужно для
    последовательного диалога.
    """

    def __init__(
        self,
        on_text: Callable[[str], None] | None = None,
        *,
        prompt: str = "Вы> ",
        banner: bool = True,
    ) -> None:
        self._on_text = on_text
        self._prompt = prompt
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._muted = False
        # True, пока предыдущая реплика не обработана.
        self.busy = threading.Event()
        self._idle = threading.Event()   # простаивает
        self._idle.set()
        if banner:
            print(BANNER)

    # --- синхронизация с обработкой ---------------------------------------

    def mark_busy(self) -> None:
        """Сообщить, что реплика принята в обработку."""
        self._idle.clear()

    def mark_done(self) -> None:
        """Сообщить, что обработка завершена — можно читать следующую строку."""
        self._idle.set()

    @property
    def idle(self) -> threading.Event:
        return self._idle

    # --- ввод --------------------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._reader, daemon=True, name="sinaps-stdin")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Разбудить поток, если он ждёт завершения обработки.
        self.mark_done()

    def _reader(self) -> None:
        while not self._stop.is_set():
            # Ждём завершения обработки ПРЕДЫДУЩЕЙ реплики.
            #
            # Именно цикл, а не `wait(timeout=...)`: с таймаутом поток
            # срывается в чтение следующей строки через 0.25 с, реплика
            # приходит в занятую сессию и молча теряется — пользователь
            # видит «обрабатываю…» и не получает ответа.
            while not self._stop.is_set() and not self._idle.is_set():
                self._idle.wait(timeout=0.1)

            if self._stop.is_set():
                return
            try:
                line = input(self._prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                if self._on_text:
                    self._on_text("/quit")
                return
            line = line.strip()
            if line:
                self._emit(line)

    def _emit(self, line: str) -> None:
        line = (line or "").strip()
        if not line or self._on_text is None:
            return
        try:
            self._on_text(line)
        except Exception:
            log.exception("Ошибка обработки реплики из консоли")

    def submit(self, text: str) -> None:
        """Отправить реплику из кода (например, из теста или трея)."""
        self._emit(text)

    # --- вывод -------------------------------------------------------------

    def echo_user(self, text: str) -> None:
        print(f"{self._prompt}{text}")

    def echo_assistant(self, text: str, *, via: str = "") -> None:
        tail = f"  ({via})" if via else ""
        print(f"Синапс> {text}{tail}")

    def info(self, text: str) -> None:
        print(f"· {text}")

    def error(self, text: str) -> None:
        print(f"! {text}", file=sys.stderr)

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        self.info("Озвучка " + ("выключена" if muted else "включена"))


class ConsoleCommandRouter:
    """Разбирает команды, начинающиеся с `/`, и передаёт остальное в ассистенту.

    Вынесено отдельно от `Console`, потому что это логика, а не ввод-вывод:
    её покрывают обычные юнит-тесты без захвата stdin.
    """

    def __init__(self, handlers: dict[str, Callable[[str], None]]) -> None:
        self._handlers = handlers

    def route(self, line: str) -> bool:
        """True — команда обработана, False — это реплика ассистенту."""
        if not line.startswith("/"):
            return False
        parts = line[1:].split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        handler = self._handlers.get(name)
        if handler is None:
            handler = self._handlers.get("help")
            if handler:
                handler(f"Неизвестная команда /{name}")
                return True
            return False

        handler(arg)
        return True


async def wait_for_input(prompt: str = "> ") -> str:
    """Асинхронный ввод одной строки (для `--interactive` режима)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))
