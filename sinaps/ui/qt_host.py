"""Запуск с треем: Qt в главном потоке, asyncio — в отдельном.

Почему именно так
-----------------
Qt на Windows требует, чтобы его цикл событий крутился в главном
потоке: иначе сломываются перетаскивание, контекстное меню и
уведомления. asyncio, наоборот, не любит, когда его перехватывают из
другого потока без `call_soon_threadsafe`.

Отсюда раскладка:

    главный поток   QApplication, трей, окна, Qt-сигналы
    рабочий поток   event loop asyncio, микрофон, модели, навыки

Обмен идёт только через `Bridge`:
  * asyncio -> Qt: испускаем сигнал; Qt сам кладёт его в очередь
    приёмника, потому что потоки разные;
  * Qt -> asyncio: `loop.call_soon_threadsafe`.

Если Qt недоступен (нет графической сессии), приложение обязано
сообщить об этом и выйти с понятным кодом, а не падать в трей.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Сколько ждём готовности ассистента, прежде чем показать трей.
STARTUP_TIMEOUT_S = 90.0


def qt_available() -> tuple[bool, str]:
    """Можно ли в принципе запустить интерфейс.

    Проверять доступность трея здесь НЕЛЬЗЯ: `QSystemTrayIcon` требует
    уже созданного `QApplication`, а он появляется позже, в `run()`.
    Обращение к Qt без экземпляра приложения роняет процесс наглухо,
    без сообщения и без трассировки — Qt просто падает.
    """
    if sys.platform != "win32":
        return False, "интерфейс рассчитан на Windows"
    try:
        import PySide6.QtWidgets  # noqa: F401
    except ImportError as exc:
        return False, f"PySide6 не установлен: {exc}"
    return True, ""


@dataclass
class StartupResult:
    """Итог запуска: успех, ошибка или «просили выйти»."""

    ok: bool = False
    error: str = ""
    started_at: float = 0.0


class QtHost:
    """Держит QApplication, трей и окна, а также поток с event loop."""

    def __init__(self, cfg, *, console: bool = False,
                 assembler: Callable[..., Any] | None = None,
                 no_audio: bool = False) -> None:
        self.cfg = cfg
        self.console = console
        self._assembler = assembler
        self._no_audio = no_audio
        #: Что показать пользователю после старта (пусто — нечего).
        self._notice = ""

        self.app = None
        self.tray = None
        self.history_window = None
        self.settings_window = None
        self.panel = None

        self.loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._startup = StartupResult()
        self._ready = threading.Event()
        self._quit = threading.Event()
        self._runtime = None
        self._paused = False
        self._speak_enabled = True
        self._store = None
        self._state = "idle"

    # --- запуск -----------------------------------------------------------

    def run(self) -> int:
        """Запустить и блокировать до выхода. Возвращает код процесса."""
        from PySide6.QtWidgets import QApplication

        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setApplicationName("Sinaps")

        # Трей проверяем только после создания QApplication: Qt без
        # экземпляра приложения падает без сообщения.
        from PySide6.QtWidgets import QSystemTrayIcon

        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("Системный трей недоступен — показываю окно статуса")
            self._startup.error = "системный трей недоступен в этой сессии"
            return 1

        # Иконка приложения для окон: та же, что в трее.
        from .tray import TrayIcon, paint_icon
        self.app.setWindowIcon(paint_icon("idle"))

        self.tray = TrayIcon()
        self.tray.show()

        self._connect_tray()

        # Готовим хранилище истории: ошибка не должна мешать запуску.
        self._store = self._make_store()

        # asyncio в отдельном потоке.
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="sinaps-asyncio", daemon=True)
        self._loop_thread.start()

        # Ждём, пока ассистент поднимется: нельзя показывать «Спит»,
        # когда на самом деле он ещё грузит модели.
        if not self._ready.wait(STARTUP_TIMEOUT_S):
            self._startup.error = ("ассистент не поднялся за "
                                   f"{STARTUP_TIMEOUT_S:.0f} с")

        if not self._startup.ok:
            self.app.quit()
            return 1

        if self.console or self._notice:
            self.tray.show_message(
                "Sinaps запущен",
                self._notice or ("Ассистент работает в фоне. "
                                 "Управление — значок в трее."),
                warning=bool(self._notice))

        return int(self.app.exec())

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        assert self.loop is not None
        try:
            self.loop.run_until_complete(self._assistant())
        except Exception as exc:                    # noqa: BLE001
            log.exception("Поток ассистента упал")
            self._startup.error = str(exc)
            self._ready.set()
        finally:
            try:
                self.loop.close()
            except Exception:
                log.debug("Не удалось закрыть event loop", exc_info=True)

    async def _assistant(self) -> None:
        """Жизненный цикл ассистента в asyncio-потоке."""
        assert self.loop is not None
        try:
            runtime = self._build()
        except Exception as exc:                    # noqa: BLE001
            self._startup.error = str(exc)
            self._ready.set()
            return

        self._runtime = runtime
        self._subscribe(runtime)
        await runtime.start()

        self._startup.ok = True
        self._startup.started_at = time.time()
        self._ready.set()
        log.info("Ассистент готов, интерфейс активен")

        # Ждём сигнала выхода.
        while not self._quit.is_set():
            await asyncio.sleep(0.2)
            await self._report_state()

        log.info("Завершаю работу по команде из интерфейса")
        await runtime.stop()

    def _build(self):
        """Получает готовый рантайм.

        Сборку графа делает `runtime.py` — правило проекта: ручная сборка
        живёт только там, а `ui` получает готовый объект.

        Отсутствие микрофона — не поломка, а другая конфигурация машины.
        Консольный режим в этом случае переходит на текст; трей обязан
        вести себя так же, иначе вместо окна с историей пользователь
        получает трассировку в никуда.
        """
        if self._assembler is not None:
            return self._assembler(self.cfg, self._want_audio())

        from ..audio.devices import has_input_device
        from ..runtime import build_assistant

        want_audio = self._want_audio()
        if want_audio and not has_input_device():
            log.warning("Микрофон не найден — трей работает без голоса")
            self._notice = ("Микрофон не найден. Ассистент работает без голоса, "
                            "управление — здесь, в трее.")
            want_audio = False
        elif want_audio:
            self._notice = ""

        return build_assistant(self.cfg, with_audio=want_audio)

    def _want_audio(self) -> bool:
        return not self._no_audio

    def _make_store(self):
        from ..memory.store import default_store

        try:
            store = default_store(self.cfg.privacy.keep_history_days)
            store.prune()
            return store
        except Exception as exc:                    # noqa: BLE001
            log.warning("История недоступна: %s", exc)
            return None

    # --- события ассистента -> трей ----------------------------------------

    def _subscribe(self, runtime) -> None:
        """Подписываемся на состояние, чтобы индикатор трея был живым.

        Историю здесь НЕ пишем. Её пишет `ConversationPipeline` — один
        писатель на программу. Если подписаться ещё и тут, каждый обмен
        сохранится дважды, а в историю попадут служебные реплики вроде
        «секунду, я ещё отвечаю», которые не являются разговором.
        """
        assert self.tray is not None
        bus = runtime.bus
        bridge = self.tray.bridge

        async def _on_state(event) -> None:
            state = event.payload.get("new", "")
            self._state = state
            bridge.status_changed.emit(state, event.payload.get("reason", ""))
            await self._report_state()

        async def _on_error(event) -> None:
            bridge.status_changed.emit(
                "error", str(event.payload.get("detail", ""))[:120])

        async def _on_panel(event) -> None:
            # Навык попросил показать панель. Событие приходит из
            # asyncio-потока, поэтому отдаём Qt-сигнал: он сам
            # разложится в очередь приёмника. Обращаться к виджету
            # из этого потока нельзя — Qt к этому не готов.
            bridge.panel_requested.emit()

        bus.on("state_changed")(_on_state)
        bus.on("panel_requested")(_on_panel)
        bus.on("error")(_on_error)

    async def _report_state(self) -> None:
        """Отправляет в трей полное состояние (пауза, озвучка, счётчик)."""
        if self.tray is None:
            return
        from .tray import TrayView

        count = 0
        if self._store is not None:
            try:
                count = int(self._store.stats().get("total", 0))
            except Exception:
                count = 0

        # Сигнал испускаем из asyncio-потока; Qt разложит его в очередь.
        self.tray.bridge.view_changed.emit(TrayView(
            state=self._state,
            detail="",
            listening=not self._paused,
            speak_enabled=self._speak_enabled,
            history_count=count,
        ))

    # --- трей -> ассистент -------------------------------------------------

    def _call_async(self, coro_factory: Callable[[], Any]) -> None:
        """Безопасно отправляет действие в asyncio-поток.

        Вызов выполняется уже внутри event loop, а не заранее: фабрика
        может вернуть корутину, готовую задачу или вообще ничего.
        Раньше результат сразу скармливался `ensure_future`, и
        `handle_text_input` (возвращает `Task`) на этом падал — кнопка
        «Попробовать» в панели не работала бы.
        """
        if self.loop is None or self.loop.is_closed():
            return

        def _invoke() -> None:
            try:
                result = coro_factory()
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:                         # noqa: BLE001
                log.exception("Действие из интерфейса упало")

        try:
            self.loop.call_soon_threadsafe(_invoke)
        except RuntimeError:
            log.debug("Event loop закрыт — действие из интерфейса пропущено")

    def _connect_tray(self) -> None:
        assert self.tray is not None
        bridge = self.tray.bridge

        bridge.pause_toggled.connect(self._toggle_pause)
        bridge.speak_toggled.connect(self._toggle_speak)
        bridge.history_requested.connect(self._show_history)
        bridge.settings_requested.connect(self._show_settings)
        bridge.capabilities_requested.connect(self._show_panel)
        bridge.panel_requested.connect(self._show_panel)
        bridge.status_window_requested.connect(self._show_history)
        bridge.clear_history_requested.connect(self._clear_history)
        bridge.quit_requested.connect(self._quit_app)

    # --- панель возможностей ------------------------------------------------

    def _panel_data(self):
        """Собирает панель из реестра навыков.

        Реестр живёт в asyncio-потоке, а панель — в Qt-потоке. Данные
        пересекают границу потоков простым объектом без Qt, поэтому
        пересчёт здесь безопаснее, чем доступ к реестру из виджета.
        """
        from .panel_data import build_cards

        registry = getattr(self._runtime, "registry", None)
        if registry is None:
            log.warning("Панель: реестр навыков недоступен")
            return None
        try:
            return build_cards(registry.specs)
        except Exception:                             # noqa: BLE001
            log.exception("Панель: не удалось собрать данные")
            return None

    def _show_panel(self, *_args) -> None:
        """Показывает панель. Вызывается из трея и от навыка."""
        data = self._panel_data()
        if data is None or not data.groups:
            self._notify("Sinaps", "Не удалось собрать список возможностей.")
            return

        if self.panel is None:
            from .panel import Panel

            self.panel = Panel()
            # Фраза уходит в ассистента так, будто её сказали голосом.
            self.panel.phrase_chosen.connect(self._say_to_assistant)

        self.panel.set_data(data)
        self.panel.show()

    def _say_to_assistant(self, phrase: str) -> None:
        """Отправляет фразу из панели в обычный конвейр разговора."""
        runtime = self._runtime
        if runtime is None or runtime.conversation is None:
            return
        self._call_async(lambda: runtime.conversation.handle_text_input(phrase))

    def _toggle_pause(self, paused: bool) -> None:
        self._paused = paused
        if self._runtime is not None and self._runtime.listener is not None:
            self._call_async(lambda: self._runtime.listener.set_paused(paused))
        if self.tray is not None:
            self.tray.bridge.status_changed.emit(
                "paused" if paused else "idle", "")

    def _toggle_speak(self, enabled: bool) -> None:
        self._speak_enabled = enabled
        if self._runtime is not None:
            self._runtime.conversation.speak_enabled = enabled

    def _clear_history(self) -> None:
        if self._store is None:
            return
        try:
            self._store.clear()
        except Exception as exc:                    # noqa: BLE001
            log.warning("История: очистка не удалась (%s)", exc)
        if self.history_window is not None:
            self.history_window._reload()  # noqa: SLF001

    # --- окна --------------------------------------------------------------

    def _show_history(self) -> None:
        from .windows import HistoryWindow

        if self.history_window is None:
            if self._store is None:
                self._notify("История", "Хранилище недоступно.")
                return
            self.history_window = HistoryWindow(self._store)
        self.history_window.show()
        self.history_window.raise_()
        self.history_window.activateWindow()

    def _show_settings(self) -> None:
        from ..audio.devices import list_input_devices
        from ..platform import autostart
        from .windows import SettingsWindow

        if self.settings_window is None:
            self.settings_window = SettingsWindow(
                self.cfg,
                audio_devices=list_input_devices,
                voices=self._voice_names,
                on_apply=self._on_settings_apply,
                autostart_state=autostart.is_enabled,
                on_autostart=autostart.set_enabled,
            )
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    @staticmethod
    def _voice_names() -> list[str]:
        """Голоса Silero, если движок доступен."""
        try:
            from ..tts.silero_tts import VOICES

            return list(VOICES)
        except Exception:
            return []

    def _on_settings_apply(self, changes: dict) -> None:
        """Применить то, что можно без перезапуска.

        Запись в `config.yaml` делает само окно настроек: сохранение не
        должно зависеть от того, дождался ли его обратный вызов.
        """
        speak = changes.get("speak_enabled")
        if speak is not None and bool(speak) != self._speak_enabled:
            self._toggle_speak(bool(speak))

        self._notify("Настройки", "Сохранено в data/config.yaml.\n"
                                 "Модель и микрофон применятся после перезапуска.")

    def _notify(self, title: str, body: str) -> None:
        if self.tray is not None:
            self.tray.show_message(title, body)

    # --- выход -------------------------------------------------------------

    def _quit_app(self) -> None:
        log.info("Выход по команде из трея")
        self._quit.set()
        # Даём asyncio-потоку завершиться, потом закрываем Qt.
        QTimer_single_shot(400, self.app.quit)


def QTimer_single_shot(ms: int, func) -> None:   # noqa: N802 - внутреннее
    """Отложенный вызов в Qt-потоке."""
    from PySide6.QtCore import QTimer

    QTimer.singleShot(ms, func)


def start_tray_mode(cfg, *, console: bool = False,
                    no_audio: bool = False) -> int:
    """Точка входа для запуска с интерфейсом.

    Возвращает код процесса. Если интерфейс невозможен — печатает
    причину и возвращает 1: молча выйти нельзя, пользователь должен
    понимать, почему трея нет.
    """
    ok, reason = qt_available()
    if not ok:
        print(f"Режим с треем недоступен: {reason}")
        print("Запусти консольный режим:  .\\run_sinaps.bat")
        return 1

    try:
        return QtHost(cfg, console=console, no_audio=no_audio).run()
    except Exception as exc:                        # noqa: BLE001
        log.exception("Интерфейс не запустился")
        print(f"Не удалось запустить интерфейс: {exc}")
        print("Логи: data/logs/sinaps.log")
        return 1