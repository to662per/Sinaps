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

from ..core.config import save_config

#: Настройки, меняющие движки. Живут в `settings_view`, чтобы окно и
#: трей смотрели на один список: два перечня разъедутся за месяц.
from .settings_view import RESTART_MARKERS

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class _WindowBridge(QObject):
    """Мост из asyncio-потока в Qt-поток для обновления окна.

    Отдельный объект, а не `Bridge` из трея: трей может не создаться
    (в тестах и там, где системного трея нет), а окно обновлять всё
    равно придётся.

    Сигнал вместо `QTimer.singleShot` — не украшение, а требование Qt:
    таймер создаётся в том потоке, из которого вызван, и из
    asyncio-потока, где Qt-цикла нет, он просто не срабатывает. Ни
    ошибки, ни в��лога: окно молча остаётся прежним.

    Ровно это и происходило. История писалась в базу, а вкладка
    «История» в окне оставалась пустой — при этом прямое присваивание
    работало, и проверка в тесте с главным потоком проходила.
    """

    call = Signal(object)

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
                 no_audio: bool = False,
                 cfg_path: str | None = None) -> None:
        self.cfg = cfg
        #: Откуда конфигурацию читали. Без этого пути `save_config(cfg)`
        #: писала в боевой `data/config.yaml`, даже если читала из
        #: другого места, — а правка из окна исчезала из файла, который
        #: человек на самом деле открывает.
        self.cfg_path = cfg_path
        self.console = console
        self._assembler = assembler
        self._no_audio = no_audio
        #: Что показать пользователю после старта (пусто — нечего).
        self._notice = ""

        self.app = None
        self.tray = None
        self.panel = None
        self.main_window = None

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
        self._window_bridge: _WindowBridge | None = None

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

        # Мост создаётся здесь, в Qt-потоке, и только здесь: QObject
        # нельзя создавать до QApplication, а слот — в другом потоке.
        self._window_bridge = _WindowBridge()
        self._window_bridge.call.connect(lambda action: action())

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
            runtime = await self._build()
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

        # Приветствие после готовности, а не раньше: пока грузятся модели,
        # говорить нечем. Идёт через обычный конвейер разговора, поэтому
        # работают синтез, прерывание и пауза — отдельный путь не нужен.
        if self.cfg.tts.greet_on_start:
            asyncio.get_running_loop().call_later(
                1.0, lambda: runtime.conversation.handle_text_input("привет"))

        # Ждём сигнала выхода.
        while not self._quit.is_set():
            await asyncio.sleep(0.2)
            await self._report_state()

        log.info("Завершаю работу по команде из интерфейса")
        await runtime.stop()

    async def _build(self):
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

        return await build_assistant(self.cfg, with_audio=want_audio)

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

        async def _on_mic(event) -> None:
            # Микрофон появился после старта. Без сообщения человек
            # не поймёт, почему ассистент, который полминуты назад был
            # немым, заработал, — и решит, что это случайность.
            self._notify("Sinaps", "Микрофон подключён — слушаю")
            await self._report_state()

        bus.on("state_changed")(_on_state)
        bus.on("panel_requested")(_on_panel)
        bus.on("error")(_on_error)
        bus.on("microphone_attached")(_on_mic)

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
        bridge.main_window_requested.connect(self._show_main_window)
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

    def _show_main_window(self) -> None:
        """Показать главное окно.

        Создаётся один раз и переиспользуется: показывать заново каждый
        раз означало бы заново строить все карточки плагинов, а при
        двадцати плагинах это заметная задержка.

        Данные плагинов приходят из asyncio-потока, поэтому каждый раз
        перед показом окно просит снимок. Это дешевле, чем подписка на
        события с перерисовкой на каждый чих: плагинов мало, а список
        меняется редко.
        """
        if self.main_window is None:
            from .main_window import MainWindow

            self.main_window = MainWindow()
            self.main_window.phrase_entered.connect(self._say_to_assistant)
            self.main_window.listen_requested.connect(self._open_listen)
            self.main_window.reload_requested.connect(
                self._reload_plugin_from_ui)
            self.main_window.approve_requested.connect(
                self._approve_plugin_from_ui)
            self.main_window.revoke_requested.connect(
                self._revoke_plugin_from_ui)
            self.main_window.toggle_plugin_requested.connect(
                self._toggle_plugin_from_ui)
            self.main_window.history_query_changed.connect(
                self._on_history_query)
            self.main_window.history_export_requested.connect(
                self._on_history_export)
            self.main_window.history_clear_requested.connect(
                self._on_history_clear)
            self.main_window.settings_changed.connect(
                self._on_setting_changed)

        self._fill_settings(self.main_window)
        self._refresh_main_window()
        self.main_window.request_history()
        self.main_window.show()
        self.main_window.raise_()

    def _fill_settings(self, window) -> None:
        """Показать настройки и список устройств.

        Окно передаётся явно, а не берётся из `self`: иначе вызов
        молча вышел бы, покидая окно с пустыми списками, и человек
        решил бы, что микрофонов в системе нет.

        Список микрофонов опрашивается Portaudio, а это может занять
        десятки миллисекунд и на худой машине — заметно. Поэтому он
        наполняется один раз, при создании окна, а не на каждом
        показе: подключённый микрофон за время жизни окна меняется
        редко, а тормозить открытие окна — часто.
        """
        if window is None:
            return

        try:
            window.set_settings(self.cfg)
        except Exception:                             # noqa: BLE001
            log.exception("Главное окно: не удалось показать настройки")

        try:
            from ..audio.devices import (list_input_devices,
                                          list_output_devices)

            window.set_devices(list_input_devices(), list_output_devices())
        except Exception:                             # noqa: BLE001
            # Нет Portaudio или нет звуковой карты: список останется
            # пустым, работа с клавиатуры по-прежнему возможна.
            log.warning("Не удалось получить список устройств звука",
                        exc_info=True)

    def _refresh_main_window(self) -> None:
        """Обновить снимок плагинов и подпись состояния."""
        if self.main_window is None:
            return
        runtime = self._runtime
        if runtime is None:
            self.main_window.set_plugin_summary("Ассистент не запущен")
            return

        try:
            self.main_window.set_plugins_view(runtime.plugin_view())
        except Exception:                             # noqa: BLE001
            log.exception("Главное окно: не удалось собрать данные о плагинах")
            self.main_window.set_plugin_summary("Не удалось прочитать плагины")
            return

        try:
            status = self._state_text(runtime.session.state.value)
        except Exception:                             # noqa: BLE001
            status = runtime.session.state.value
        self.main_window.set_status(status)

    # --- история ----------------------------------------------------------

    def _on_history_query(self, filters: dict) -> None:
        """Показать историю по фильтрам.

        Запрос уходит в asyncio-поток: база занята там, а окно живёт в
        Qt-потоке, и обращение из одного в другое без границы кончается
        зависанием или порчей соединения sqlite.
        """
        if self._store is None:
            log.warning("История: хранилище недоступно")
            self._notify("История", "Хранилище недоступно")
            return

        log.info("История: запрос %r", filters)

        async def _load() -> None:
            entries = self._store.history(**filters)
            stats = self._store.history_stats()
            summary = f"показано {len(entries)}"
            # Без этой строки в логе пустой список неотличим от
            # «запрос не дошёл»: и то, и другое выглядит одинаково —
            # пустая вкладка. С этим видно, где обрыв.
            log.info("История: показано записей %d", len(entries))
            self._post_to_window(
                lambda: self.main_window.set_history(entries, stats, summary))

        self._call_async(_load)

    def _on_history_export(self, _path: str, filters: dict) -> None:
        """Выгрузить историю в файл рядом с базой."""
        if self._store is None:
            self._notify("История", "Хранилище недоступно")
            return

        async def _export() -> None:
            from datetime import datetime

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = Path(self._store.path).parent / f"history_{stamp}.json"
            written = self._store.export(target, **filters)
            self._post_to_window(
                lambda: self._notify(
                    "История", f"Выгружено записей: {written}\n{target}"))

        self._call_async(_export)

    def _on_history_clear(self) -> None:
        """Очистка истории — только голосом.

        Тридцать дней разговоров стираются одним щелчком рядом с кнопкой
        выгрузки. Подтверждение голосом здесь не формальность: восстановить
        нечего.
        """
        if self._store is None:
            return

        async def _clear() -> str:
            removed = self._store.clear()
            log.info("История очищена голосом: удалено записей %d", removed)
            self._post_to_window(
                lambda: self._refresh_main_window()
                if self.main_window is not None else None)
            return f"История очищена, удалено записей {removed}"

        self._ask_voice(
            "Очистить всю историю разговоров? Это нельзя отменить",
            _clear, "очистка истории")

    # --- настройки --------------------------------------------------------

    def _on_setting_changed(self, path: str, value) -> None:
        """Настройка изменена мышью.

        Пишется в тот же `config.yaml`, что и раньше. Отдельный файл
        означал бы два источника правды: «я поменял, а оно не
        применилось» невозможно будет отличить от «я поменял не там».
        """
        # Пишем в `self.cfg`, а не в `runtime.config`: конфигурация у
        # трея своя, и окно обязано править ту, что прочитается при
        # следующем запуске. Раньше бралась `runtime.config`, и при
        # незапущенном ассистенте (`runtime is None`) правка просто
        # исчезала: человек двигал ползунок и не получал ни ошибки,
        # ни сохранения.
        cfg = self.cfg

        # Путь `stt.model` разбирается вручную: конфигурация — вложенные
        # dataclass'ы, а не словарь, и общего способа задать поле у неё нет.
        parts = path.split(".")
        target = cfg
        for part in parts[:-1]:
            target = getattr(target, part, None)
            if target is None:
                self._notify("Настройки", f"Неизвестный раздел: {path}")
                return

        name = parts[-1]
        if not hasattr(target, name):
            self._notify("Настройки", f"Неизвестная настройка: {path}")
            return

        old = getattr(target, name)
        if old == value:
            return

        setattr(target, name, value)
        try:
            # Именно `save_config(cfg)`, а не `cfg.save()`: у
            # `SinapsConfig` такого метода нет, есть только `dumps()`.
            # Написал `cfg.save()` по привычке — и получал одну и ту же
            # ошибку при каждой правке, не замечая, что она повторяется.
            save_config(cfg, self.cfg_path)
        except Exception as exc:                      # noqa: BLE001
            log.exception("Не удалось сохранить настройку %s", path)
            self._notify("Настройки", f"Сохранить не получилось: {exc}")
            return

        log.info("Настройка %s: %r -> %r", path, old, value)

        # Применить на лету то, что можно, и сказать про остальное.
        if path == "tts.test":
            runtime = self._runtime
            if runtime is not None:
                self._call_async(
                    lambda: runtime.conversation.speak_now("Проверка голоса"))
            return
        if path in ("privacy.keep_history_days",):
            if self._store is not None:
                self._store.keep_days = int(value)
            return

        # Успешное сохранение не показывается: ползунок скорости
        # голоса шлёт значение на каждое движение мыши, и десять
        # одинаковых уведомлений подряд учат человека, что уведомления
        # от этого окна ничего не значат. Показываем только то, что
        # человек обязан знать: настройку, которая не подействует до
        # перезапуска.
        if any(path.startswith(prefix) for prefix in RESTART_MARKERS):
            self._notify(
                "Настройки",
                f"{path} = {value}\nПрименится после перезапуска ассистента.")

    def _post_to_window(self, action) -> None:
        """Выполнить действие в Qt-потоке.

        Результат приходит из asyncio-потока, а окно живёт в Qt-потоке.
        Обновлять виджет из чужого потока нельзя — Qt это не прощает и
        падает без внятного сообщения.
        """
        app = self.app
        if app is None:
            return
        bridge = self._window_bridge
        if bridge is None:
            # Окно ещё не создано: обновлять нечего. Раньше здесь
            # стоял `QTimer.singleShot(0, action)`, который из
            # asyncio-потока не срабатывает вообще — молча, без
            # ошибки и без записи в лог.
            log.debug("Окно ещё не готово — обновление пропущено")
            return
        try:
            bridge.call.emit(action)
        except Exception:                             # noqa: BLE001
            log.exception("Не удалось обновить окно")

    # --- плагины из окна ---------------------------------------------------

    def _reload_plugin_from_ui(self, name: str) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        self._notify("Плагины", f"Перезагружаю {name}…")

        async def _reload() -> bool:
            ok = await runtime.reload_plugin(name)
            self._refresh_main_window()
            if not ok:
                self._notify(
                    "Плагины",
                    f"Не удалось перезагрузить {name}. Подробности в журнале.")
            return ok

        self._call_async(_reload)

    def _approve_plugin_from_ui(self, name: str) -> None:
        """Одобрить права прямо из окна.

        Одобрение пишется через `approvals.grant`, а не в обход него:
        файл один, иначе права из окна и права из голоса разошлись бы.
        """
        from ..plugins import approvals
        from ..plugins.manifest import permission_title

        runtime = self._runtime
        if runtime is None or runtime.plugins is None:
            return

        plugin = next((p for p in runtime.plugins.loaded if p.name == name),
                      None)
        if plugin is None:
            self._notify("Плагины", f"Плагин {name} не загружен")
            return

        try:
            approvals.grant(name, plugin.permissions)
        except Exception as exc:                      # noqa: BLE001
            log.exception("Не удалось записать одобрение для %s", name)
            self._notify("Плагины", f"Не удалось сохранить: {exc}")
            return

        granted = ", ".join(permission_title(p)
                            for p in sorted(plugin.permissions)) or "нет прав"
        log.info("Плагин %s одобрен из окна: %s", name, granted)
        self._notify("Права одобрены", f"{name}: {granted}")

        # Плагин продолжает работать с прежними правами до перезагрузки:
        # право проверяется при обращении к возможности, а не при старте.
        self._call_async(lambda: runtime.reload_plugin(name))
        self._refresh_main_window()

    def _revoke_plugin_from_ui(self, name: str) -> None:
        """Отзыв прав — только после голосового подтверждения.

        Право — это то, что плагин может делать с компьютером. Отозвать
        его кнопкой, не спросив голосом, значит оставить отзыв на
        случайный щелчок рядом с кнопкой перезагрузки.
        """
        from ..plugins.manifest import permission_title

        runtime = self._runtime
        if runtime is None or runtime.plugins is None:
            return

        plugin = next((p for p in runtime.plugins.loaded if p.name == name),
                      None)
        titles = (", ".join(permission_title(p)
                            for p in sorted(plugin.permissions))
                  if plugin is not None else "")

        async def _revoke() -> str:
            from ..plugins import approvals

            try:
                approvals.revoke(name)
            except Exception as exc:                  # noqa: BLE001
                log.exception("Не удалось отозвать права у %s", name)
                return f"Не получилось отозвать права: {exc}"

            log.info("Права плагина %s отозваны: %s", name, titles)
            self._refresh_main_window()
            return f"Права {name} отозваны"

        question = f"Отозвать права у плагина {name}?"
        if titles:
            question += f" Он сможет: {titles}."
        self._ask_voice(question, _revoke, f"отзыв прав {name}")

    def _toggle_plugin_from_ui(self, name: str, enable: bool) -> None:
        """Включить или выключить плагин без перезапуска ассистента.

        Выключение убирает команды из реестра и закрывает процесс.
        Обратное действие грузит плагин заново — иначе включение было бы
        обещанием, которое ничего не делает.
        """
        runtime = self._runtime
        if runtime is None or runtime.plugins is None:
            return

        plugin = next((p for p in runtime.plugins.loaded if p.name == name),
                      None)

        # Включение — безопасное действие: плагин начинает работать там,
        # где человек его и так поставил. Выключение — нет: оно убирает
        # команды, которые уже отвечали, и может остаться непризнанным.
        if enable:
            runtime.disabled_plugins.discard(name)
            if plugin is None:
                self._notify(
                    "Плагины",
                    f"Плагин {name} не загружен, включить нельзя")
                return
            self._call_async(lambda: runtime.reload_plugin(name))
            self._refresh_main_window()
            return

        commands = len(plugin.commands) if plugin is not None else 0

        async def _disable() -> str:
            runtime.disabled_plugins.add(name)
            if plugin is not None and plugin.holder is not None:
                runtime.registry.remove(plugin.holder)
            if plugin is not None:
                await plugin.stop(reason="stop")
            log.info("Плагин %s выключен голосом", name)
            self._refresh_main_window()
            return f"Плагин {name} выключен"

        self._ask_voice(
            f"Выключить плагин {name}? Он перестанет отвечать",
            _disable, f"выключение {name}")

    def _ask_voice(self, question: str, action, label: str) -> None:
        """Спросить голосом и выполнить действие при согласии.

        Если подтверждение уже занято — сообщаем об этом и **не**
        выполняем: человек подтвердил бы не то действие, которое
        собирался.
        """
        runtime = self._runtime
        if runtime is None:
            return

        if runtime.confirm_plugin_action(question, action, label):
            self._show_guard_prompt(question)
            return

        waiting = runtime.plugin_guard.question
        self._notify(
            "Подтверждение",
            f"Уже жду ответа: {waiting}\nСначала ответьте на этот вопрос.")

    def _show_guard_prompt(self, question: str) -> None:
        """Показать вопрос в окне и в трее.

        В окне — чтобы человек прочитал, в трее — чтобы увидел, если
        окно закрыто. Молчащий вопрос выглядит как зависание.
        """
        if self.main_window is not None:
            self.main_window.set_guard_question(question)
        self._notify("Подтвердите голосом", f"{question} Скажите «да» или «нет».")

    def _on_guard_answered(self, done: bool, text: str) -> None:
        """Ответ пришёл: убрать вопрос и сказать результат."""
        if self.main_window is not None:
            self.main_window.set_guard_question(None)
        if done:
            self._notify("Готово", text)
        else:
            self._notify("Подтверждение", text)

    @staticmethod
    def _state_text(state: str) -> str:
        """Название состояния для окна — то же, что в трее."""
        from .tray import STATES

        _, title, _ = STATES.get(state, STATES["idle"])
        return title

    def _open_listen(self) -> None:
        """Кнопка «Слушать»: активация микрофона без горячей клавиши.

        Микрофон пропадает нечасто — на ноутбуке может отвалиться кнопка
        или её съест софт, — и тогда включить ассистента нечем. Вторая
        дверь нужна.
        """
        runtime = self._runtime
        if runtime is None:
            return
        if runtime.listener is None:
            self._notify("Sinaps", "Микрофон не используется")
            return
        async def _activate() -> bool:
            opened = await runtime.listener.activate(source="button")
            if not opened:
                reason = "пауза включена" if runtime.session.state else ""
                self._notify("Sinaps", f"Сейчас не слушаю{': ' + reason if reason else ''}")
            return opened

        self._call_async(_activate)

    def _show_history(self) -> None:
        """Открыть историю.

        Ведёт в главное окно на вкладку «История», а не в отдельное
        окно `HistoryWindow`. Два места для одного и того же хуже
        одного: старое окно работало, а вкладка молча показывала
        пустоту, и человек решил, что история пишется «куда-то» и
        пропадает. Раз история удалена из старого места, и в трее
        должен быть тот же путь, что и в окне.
        """
        self._show_main_window()
        if self.main_window is not None:
            self.main_window.show_section("history")

    def _show_settings(self) -> None:
        """Открыть настройки.

        Ведёт на вкладку «Настройки» главного окна, а не в отдельное
        окно `SettingsWindow` — по той же причине, что и в
        `_show_history`. Настройки в том числе меняют автозапуск, за
        который отвечал отдельный диалог, поэтому это единственное
        место, где стартовое окно ещё используется: `_fill_settings`
        вызывает его сам.
        """
        self._show_main_window()
        if self.main_window is not None:
            self.main_window.show_section("settings")

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
        from ..core.config import config_path

        return QtHost(cfg, console=console, no_audio=no_audio,
                      cfg_path=str(config_path())).run()
    except Exception as exc:                        # noqa: BLE001
        log.exception("Интерфейс не запустился")
        print(f"Не удалось запустить интерфейс: {exc}")
        print("Логи: data/logs/sinaps.log")
        return 1