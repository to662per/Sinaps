"""Контейнер зависимостей и запуск ядра.

Собирает все движки из конфига в один граф и передаёт его конвейерам.
Отдельная функция `build_runtime` существует ради тестов: она позволяет
подсунуть заглушки вместо микрофона и моделей.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

# Импорт на уровне модуля, а не под TYPE_CHECKING: `VoiceGuard` нужен в
# `__post_init__` и в обоих сборщиках. Под TYPE_CHECKING пришлось бы
# дублировать или прятать реальную зависимость.
from .core.guard import VoiceGuard

from .audio.capture import AudioCapture  # noqa: F401  (re-export для удобства импорта)
from .audio.player import AudioPlayer
from .conversation.listening import ListeningPipeline
from .conversation.pipeline import ConversationPipeline, Responder
from .core.bus import EventBus, EventType
from .core.config import SinapsConfig, get_config
from .core.metrics import Metrics
from .core.offline import report as network_report
from .core.session import SessionController
from .platform.base import PlatformCapabilities
from .platform.detect import build_platform
from .skills.engine import SkillEngine, build_default_registry
from .skills.registry import SkillRegistry
from .plugins.loader import (
    IDLE_TIMEOUT_S, load_all, plugin_stats, reap_idle, reload_plugin,
    replace_plugin, LoadReport)
from .stt.base import STTEngine, load_engine as load_stt
from .tts.base import TTSEngine, load_engine as load_tts
from .ui.trigger import ActivationTrigger, Trigger, parse_trigger

log = logging.getLogger(__name__)

#: Как часто спрашивать Portaudio, появился ли микрофон. Секунда — с
#: запасом: человек подключает наушники и ждёт отклика, а не считает
#: миллисекунды. Чаще опрашивать незачем, перечисление устройств не
#: бесплатное, а реже — ощутимая задержка.
MIC_WATCH_INTERVAL_S = 1.0


@dataclass
class Runtime:
    """Всё живое, что нужно ассистенту."""

    config: SinapsConfig
    bus: EventBus
    session: SessionController
    metrics: Metrics
    capture: AudioCapture | None
    player: AudioPlayer
    stt: STTEngine
    tts: TTSEngine
    listener: ListeningPipeline | None
    conversation: ConversationPipeline
    registry: SkillRegistry | None = None
    platform: "PlatformCapabilities | None" = None
    trigger: "ActivationTrigger | None" = None

    #: Что произошло при загрузке плагинов. Раньше отчёт был локальной
    #: переменной в `build_skills_runtime` и терялся на выходе из функции:
    #: `reap_idle` и `stop_all` были написаны, покрыты тестами на
    #: загрузчике — и ни разу не вызваны из приложения. Держать отчёт
    #: здесь — единственный способ до них добраться.
    plugins: "LoadReport | None" = None

    #: Фоновая задача, закрывающая простоящие процессы плагинов.
    _reaper: asyncio.Task | None = None

    #: Задача, ждущая микрофон, если его не было при старте.
    _mic_watcher: asyncio.Task | None = None

    #: Плагины, выключенные пользователем. Их команды убраны из
    #: реестра, поэтому перезагрузка их не запустит.
    disabled_plugins: set[str] = field(default_factory=set)

    #: Подтверждение голосом для отзыва прав и выключения плагина.
    #: Объект, а не поле настраиваемое снаружи: очередь подтверждений
    #: должна быть одна на весь ассистент, иначе окно и голос спрашивали
    #: бы одновременно о разном.
    _plugin_guard: "VoiceGuard | None" = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._plugin_guard is None:
            from .core.guard import VoiceGuard

            self._plugin_guard = VoiceGuard()

    #: Текст проблемы с перехватом клавиш/мыши, если он не заработал.
    trigger_error: str | None = None

    async def start(self) -> None:
        """Готовим движки.

        Модели прогреваются в фоне: первый запрос по холодной модели идёт
        на несколько секунд дольше, и пользователь решит, что ассистент
        «завис». Прогрев не блокирует запуск.
        """
        if self.listener is not None:
            # Микрофон открываем ПОСЛЕ загрузки моделей. Пока грузятся
            # Silero TTS и faster-whisper, главный поток держит GIL минутами,
            # а аудиоколбэк PortAudio живёт в отдельном C-потоке и без GIL
            # работать не может: буфер переполняется, кадры выбрасываются.
            # На практике это выглядит как «микрофон не работает» ровно в
            # первые секунды после запуска — когда пользователь и проверяет.
            #
            # Бонус: первая команда уходит сразу, а не платит за холодную
            # модель несколько секунд.
            log.info("Прогреваю модели — это может занять несколько секунд...")
            await self.warmup_models()
            await self.listener.start()
            if self.trigger is not None:
                self.trigger_error = self.trigger.start()
                if self.trigger_error:
                    log.warning("Активация не работает: %s", self.trigger_error)
        else:
            # Без микрофона ждать незачем: GIL никому не мешает.
            self.start_background_warmup()
            # ...но микрофон может появиться через минуту. Молчащий
            # ассистент без объяснения — худший вариант, поэтому ждём
            # подключения и поднимаем контур сами.
            self._start_mic_watcher()

        self._start_plugin_reaper()

    def _start_plugin_reaper(self) -> None:
        """Поднять фоновое закрытие простоящих плагинов.

        Отдельная задача, а не вызов после каждой реплики: уборка нужна
        именно тогда, когда никто не говорит. Проверять простой только
        после очередного запроса — значит не убирать никогда, потому что
        простой и означает отсутствие запросов.
        """
        if self.plugins is None or not self.plugins.loaded:
            return
        if self._reaper is not None and not self._reaper.done():
            # Повторный `start()` не должен плодить вторую уборку:
            # старая задача осталась бы жить и гасилась бы только при
            # остановке — а её в `self._reaper` уже не было бы.
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reaper = loop.create_task(self._reap_idle_forever())

    async def _reap_idle_forever(self) -> None:
        """Шаг уборки повторяется, пока задача не отменена."""
        # Шаг вчетверо меньше порога: иначе плагин, которым пользовались
        # в момент X, закрылся бы не раньше чем через 240 с — вдвое
        # дольше объявленного. Тело шага — сравнение одного float.
        tick = max(1.0, IDLE_TIMEOUT_S / 4)
        while True:
            await asyncio.sleep(tick)
            try:
                if self.plugins is not None:
                    await reap_idle(self.plugins)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — уборка не должна ронять ассистента
                log.warning("Уборка плагинов не удалась", exc_info=True)

    async def warmup_models(self) -> None:
        """Дождаться готовности моделей.

        Возвращает управление тогда, когда уже можно говорить.
        """
        loop = asyncio.get_running_loop()
        for engine, name in ((self.tts, "TTS"), (self.stt, "STT")):
            try:
                await loop.run_in_executor(None, engine.warmup)
                log.info("%s прогрет и готов к работе", name)
            except Exception:
                # Прогрев не прошёл: пусть первый запрос попробует сам,
                # но микрофон всё равно откроем — иначе голос не заработает.
                log.warning("Прогрев %s не удался — первый запрос будет медленнее",
                            name, exc_info=True)

    def start_background_warmup(self) -> None:
        """Запускает прогрев моделей, не дожидаясь результата."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        loop.create_task(self.warmup_models())

    async def stop(self) -> None:
        if self._mic_watcher is not None and not self._mic_watcher.done():
            self._mic_watcher.cancel()
            try:
                await self._mic_watcher
            except asyncio.CancelledError:
                pass
            self._mic_watcher = None
        if self.listener is not None:
            await self.listener.stop()
        if self.trigger is not None:
            self.trigger.stop()
        self.player.cancel()
        await self.stop_plugins()

    # --- микрофон, появившийся позже ---------------------------------------

    def _start_mic_watcher(self) -> None:
        """Следить за появлением микрофона, если его не было при старте.

        Решение «микрофона нет» принималось один раз и на весь сеанс:
        `build_skills_runtime(with_audio=False)` не собирал контур
        прослушивания, и подключение микрофона уже ничего не меняло —
        ассистент молчал до перезапуска. Люди подключают наушники с
        микрофоном через минуту после запуска, и «работает только если
        запустить заново» — худший ответ из возможных.

        Проверка идёт раз в `MIC_WATCH_INTERVAL_S`:Portaudio
        перечисляет устройства дёшево, но не настолько дёшево, чтобы
        спрашивать в цикле чаще.
        """
        if self.listener is not None:
            return
        if self._mic_watcher is not None and not self._mic_watcher.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._mic_watcher = loop.create_task(
            self._watch_microphone_forever(), name="sinaps-mic-watch")

    async def _watch_microphone_forever(self) -> None:
        """Ждать микрофон и достроить контур, когда он появится."""
        while self.listener is None:
            await asyncio.sleep(MIC_WATCH_INTERVAL_S)
            try:
                attached = await self.attach_microphone()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — слежение не должно ронять ассистента
                log.warning("Проверка микрофона не удалась", exc_info=True)
                continue
            if attached:
                log.info("Микрофон появился — контур прослушивания запущен")
                return

    async def attach_microphone(self) -> bool:
        """Достроить контур прослушивания, если микрофон появился.

        Возвращает `True`, когда контур поднят. Повторный вызов, когда
        микрофон уже есть, ничего не делает и возвращает `False`: две
        задачи прослушивания звучали бы одновременно и забирали бы
        звук друг у друга.

        Контур собирается из тех же кирпичиков, что и при старте, —
        иначе «работает после переподключения» и «работает с самого
        начала» были бы разными программами.
        """
        if self.listener is not None:
            return False

        from .audio.devices import has_input_device

        if not has_input_device():
            return False

        cfg = self.config

        try:
            from .audio.source import make_source

            capture = make_source(
                None,
                sample_rate=cfg.audio.sample_rate,
                frame_ms=cfg.audio.frame_ms,
                pre_roll_ms=cfg.audio.pre_roll_ms,
                device=cfg.audio.input_device,
                latency=cfg.audio.latency,
            )
        except Exception as exc:  # noqa: BLE001
            # Устройство появилось, но открыть его не вышло: драйвер,
            # звуковая карта занята другой программой. Следующая
            # проверка через интервал попробует снова.
            log.warning("Микрофон найден, но не открывается: %s", exc)
            return False

        listener = ListeningPipeline(
            capture=capture, stt=self.stt, vad_engine=cfg.vad.engine,
            session=self.session, bus=self.bus, metrics=self.metrics,
            vad_threshold=cfg.vad.threshold, silence_ms=cfg.vad.silence_ms,
            min_speech_ms=cfg.vad.min_speech_ms,
            max_utterance_s=cfg.vad.max_utterance_s,
            listen_timeout_s=cfg.wake.listen_timeout_s,
            require_activation=cfg.wake.mode == "hotkey",
            min_confidence=cfg.stt.min_confidence,
            record_audio=cfg.privacy.record_audio,
        )

        try:
            await listener.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("Контур прослушивания не запустился: %s", exc)
            try:
                await capture.stop()
            except Exception:  # noqa: BLE001
                log.debug("Не удалось закрыть микрофон после сбоя",
                          exc_info=True)
            return False

        self.capture = capture
        self.listener = listener

        if cfg.wake.mode == "hotkey":
            trigger = build_trigger(cfg, listener, loop=_current_loop())
            self.trigger = trigger
            self.trigger_error = trigger.start()
            if self.trigger_error:
                log.warning("Активация не работает: %s", self.trigger_error)

        try:
            await self.bus.emit(EventType.MICROPHONE_ATTACHED)
        except Exception:  # noqa: BLE001 — событие не обязано быть услышано
            log.debug("Событие подключения микрофона не доставлено",
                      exc_info=True)

        return True

    async def reload_plugin(self, name: str) -> bool:
        """Перезагрузить плагин по имени, без перезапуска ассистента.

        Нужно автору плагина на этапе отладки: раньше правка кода
        требовала перезапуска ассистента целиком, а это минуты.

        Возвращает `False`, если плагина с таким именем нет. Ошибка
        загрузки не поднимается наружу: плагин остаётся выключенным,
        причина — в журнале, а ассистент продолжает работать.
        """
        if self.plugins is None:
            return False

        plugin = next((p for p in self.plugins.loaded if p.name == name), None)
        if plugin is None:
            log.warning("Перезагрузка: плагин %s не загружен", name)
            return False

        folder = plugin.process.folder if plugin.process is not None else None
        if folder is None:
            log.warning("Перезагрузка: у плагина %s нет папки", name)
            return False

        try:
            fresh = await reload_plugin(plugin, folder, self.registry, self.bus)
        except Exception as exc:  # noqa: BLE001 — чужая папка с чужим кодом
            log.error("Перезагрузка плагина %s не удалась: %s: %s",
                      name, type(exc).__name__, exc)
            return False

        replace_plugin(self.plugins, plugin, fresh)
        log.info("Плагин %s перезагружен", name)
        return True

    def plugin_names(self) -> tuple[str, ...]:
        """Имена загруженных плагинов — для консоли и трея."""
        if self.plugins is None:
            return ()
        return tuple(plugin.name for plugin in self.plugins.loaded)

    def plugin_view(self):
        """Снимок состояния плагинов для интерфейса.

        Снимок, а не ссылка на живые объекты: окно живёт в Qt-потоке,
        плагины — в asyncio-потоке, и отдавать первому второе нельзя.
        """
        from .plugins.view import build_plugin_view

        if self.plugins is None:
            from .plugins.view import PluginView

            return PluginView()
        return build_plugin_view(
            self.plugins,
            disabled=frozenset(self.disabled_plugins),
        )

    async def stop_plugins(self) -> None:
        """Остановить фоновую уборку и закрыть процессы плагинов.

        Порядок важен: уборку гасим до закрытия процессов, иначе она
        успеет сработать на уже остановленном ассистенте. Отменённую
        задачу обязательно ждём — иначе на выходе будет «Task was
        destroyed but it is pending».
        """
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None
        if self._plugin_guard is not None:
            # Незакрытый вопрос при выходе бессмысленен: отвечать будет
            # некому, а действо могло бы выполниться на следующем
            # запуске вместе с чужим «да».
            self._plugin_guard.cancel()
        if self.plugins is not None:
            # `reason="stop"`: плагин погашен нарочно, а не уснул по
            # простоям. По событию подписчик поймёт, что это конец
            # сеанса, а не освобождение памяти посреди работы.
            await self.plugins.stop_all(reason="stop")

    def describe(self) -> str:
        """Человекочитаемое состояние — для стартового экрана и трея."""
        cfg = self.config

        # Устройство известно только после загрузки модели: до этого
        # `device` содержит значение по умолчанию и вводит в заблуждение.
        model = self.stt._model  # noqa: SLF001 - намеренно: это диагностика
        if model is None:
            stt_state = "загрузится при первом запросе"
        else:
            stt_state = f"на {self.stt.device}"

        return (
            f"Sinaps готов\n"
            f"  STT      : {self.stt.name} ({cfg.stt.model}, язык {cfg.stt.language}) {stt_state}\n"
            f"  TTS      : {self.tts.name} (голос {getattr(self.tts, 'voice', '-')}, "
            f"{self.tts.sample_rate} Гц)\n"
            f"  VAD      : {cfg.vad.engine} (порог {cfg.vad.threshold}, тишина {cfg.vad.silence_ms} мс)\n"
            f"  Микрофон : {self.capture_label()}\n"
            f"  Режим    : {network_report()}\n"
            f"  Плагины  : {self.plugins_label()}\n"
            f"  Сессия   : {self.session.state.value}"
        )

    def plugin_guard(self) -> "VoiceGuard":
        """Голосовое подтверждение действий с плагинами.

        Отзыв прав и выключение — те же опасные действия, что выключение
        компьютера, только с меньшими последствиями. Подтверждаются так
        же, голосом.
        """
        from .core.guard import VoiceGuard

        return self._plugin_guard

    def confirm_plugin_action(self, question: str, action, label: str = ""
                              ) -> bool:
        """Запросить подтверждение действия с плагином.

        `False` — подтверждение уже занято: ждёт ответа на другой
        вопрос. Возвращать «отменено» молча нельзя, человек подумал бы,
        что действие выполнилось.
        """
        return self._plugin_guard.request(question, action, label)

    async def answer_plugin_action(self, text: str, *, is_yes, is_no
                                   ) -> tuple[bool, str]:
        """Обработать голосовой ответ на вопрос к плагину."""
        return await self._plugin_guard.answer(text, is_yes=is_yes,
                                               is_no=is_no)

    def plugins_label(self) -> str:
        """Сколько плагинов в памяти и сколько они весят.

        Показывается и в логе, и здесь из одного источника. Отдельный
        счётчик в трее со временем разошёлся бы с тем, что написано
        в журнале, а расхождение в метриках никто не заметит.
        """
        if self.plugins is None:
            return "нет"
        stats = plugin_stats(self.plugins)
        if stats.total == 0:
            return "нет"
        return (f"{stats.total} всего, живых {stats.alive}, "
                f"спящих {stats.asleep}, {stats.rss_mb:.1f} МБ")

    def capture_label(self) -> str:
        """Что используется для ввода звука."""
        if self.capture is None:
            return "не используется (текстовый режим)"
        name = type(self.capture).__name__
        if name == "FileAudioSource":
            return f"файл {getattr(self.capture, 'path', '?')}"
        device = self.config.audio.input_device
        return f"системный ({device})" if device is None else f"{device}"


def build_stt(cfg: SinapsConfig) -> STTEngine:
    """Собирает движок распознавания по конфигу."""
    if cfg.stt.engine == "vosk":
        # Путь к модели Vosk читаем из конфига, если положили его в stt.
        model_path = getattr(cfg.stt, "vosk_model_path", "") or ""
        return load_stt("vosk", model_path=model_path, language=cfg.stt.language)

    return load_stt(
        cfg.stt.engine,
        model=cfg.stt.model,
        device=cfg.stt.device,
        compute_type=cfg.stt.compute_type,
        language=cfg.stt.language,
        beam_size=cfg.stt.beam_size,
        initial_prompt=cfg.stt.initial_prompt,
        min_dbfs=cfg.stt.min_dbfs,
    )


def build_tts(cfg: SinapsConfig) -> TTSEngine:
    return load_tts(
        cfg.tts.engine,
        voice=cfg.tts.voice,
        speed=cfg.tts.speed,
        volume=cfg.tts.volume,
        sample_rate=cfg.tts.sample_rate,
    )


def _current_loop() -> asyncio.AbstractEventLoop | None:
    """Текущий event loop, если мы внутри него.

    `build_runtime` вызывают и из асинхронного кода (`run`), и из тестов
    без цикла. Без проверки `get_running_loop` поднял бы RuntimeError.
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def build_trigger(
    cfg: SinapsConfig,
    listener: ListeningPipeline,
    loop: asyncio.AbstractEventLoop | None = None,
) -> ActivationTrigger | None:
    """  Собирает перехват клавиш/мыши, если он вообще настроен.

    Кнопка мыши и клавиша равноправны и включаются одновременно: если софт
    мыши перехватил боковую кнопку (Logitech G Hub, Razer Synapse,
    X-Mouse), ассистент остаётся управляем с клавиатуры. Обратное тоже
    верно: мышь без боковых кнопок не мешает работать с клавиатуры.
    """
    if cfg.wake.mode != "hotkey":
        return None

    activate = []
    for spec in (cfg.wake.mouse_button, cfg.wake.hotkey):
        trigger = parse_trigger(spec)
        # Дубли убираем: и мышь, и клавиша могут быть прописаны одинаково.
        if trigger is not None and trigger not in activate:
            activate.append(trigger)

    hold = parse_trigger(cfg.wake.push_to_talk, allow_modifiers_only=True)

    if not activate and hold is None:
        log.warning("Активация не настроена: укажи wake.mouse_button или wake.hotkey")
        return None

    async def _on_activate(source: str, timeout_s: float | None) -> None:
        await listener.activate(source=source, timeout_s=timeout_s)

    async def _on_deactivate(source: str) -> None:
        await listener.deactivate(reason=source)

    return ActivationTrigger(
        activate=activate,
        hold=hold,
        on_activate=_on_activate,
        on_deactivate=_on_deactivate,
        loop=loop,
    )


def build_runtime(
    cfg: SinapsConfig | None = None,
    *,
    responder: Responder | None = None,
    with_audio: bool = True,
    audio_file: str | None = None,
    require_activation: bool | None = None,
) -> Runtime:
    """Собирает весь граф.

    with_audio=False — без ввода звука (только текстовый ввод);
    audio_file      — ввод из wav вместо микрофона (нет микрофона / тесты).
    """
    cfg = cfg or get_config()

    bus = EventBus()
    session = SessionController(bus)
    metrics = Metrics()

    stt = build_stt(cfg)
    tts = build_tts(cfg)

    player = AudioPlayer(
        sample_rate=tts.sample_rate,
        device=cfg.audio.output_device,
        volume=cfg.tts.volume,
    )

    capture = None
    listener = None
    if with_audio:
        from .audio.source import make_source

        capture = make_source(
            audio_file,
            sample_rate=cfg.audio.sample_rate,
            frame_ms=cfg.audio.frame_ms,
            pre_roll_ms=cfg.audio.pre_roll_ms,
            device=cfg.audio.input_device,
            latency=cfg.audio.latency,
        )
        if require_activation is None:
            require_activation = cfg.wake.mode == "hotkey"
        listener = ListeningPipeline(
            capture=capture,
            stt=stt,
            vad_engine=cfg.vad.engine,
            session=session,
            bus=bus,
            metrics=metrics,
            vad_threshold=cfg.vad.threshold,
            silence_ms=cfg.vad.silence_ms,
            min_speech_ms=cfg.vad.min_speech_ms,
            max_utterance_s=cfg.vad.max_utterance_s,
            listen_timeout_s=cfg.wake.listen_timeout_s,
            require_activation=require_activation,
            min_confidence=cfg.stt.min_confidence,
            record_audio=cfg.privacy.record_audio,
        )

    if responder is None:
        from .conversation.pipeline import make_echo_responder

        responder = make_echo_responder()

    guard = VoiceGuard()

    conversation = ConversationPipeline(
        bus=bus,
        session=session,
        tts=tts,
        player=player,
        metrics=metrics,
        responder=responder,
        guard=guard,
    )
    conversation.install()

    trigger = None
    if listener is not None and require_activation:
        trigger = build_trigger(cfg, listener, loop=_current_loop())

    return Runtime(
        config=cfg, bus=bus, session=session, metrics=metrics,
        capture=capture, player=player, stt=stt, tts=tts,
        listener=listener, conversation=conversation,
        trigger=trigger, _plugin_guard=guard,
    )


def _make_memory(cfg: SinapsConfig):
    """Хранилище истории. Отказ не должен мешать запуску."""
    if getattr(cfg.privacy, "keep_history_days", 30) <= 0:
        log.info("История отключена в настройках")
        return None
    try:
        from .memory.store import default_store

        store = default_store(cfg.privacy.keep_history_days)
        store.prune()
        return store
    except Exception as exc:
        log.warning("История недоступна: %s", exc)
        return None


def build_assistant(cfg: SinapsConfig | None = None, *,
                    with_audio: bool = True) -> Runtime:
    """Готовая сборка для интерактивной работы: навыки + аудио.

    Существует, чтобы ни консольный запуск, ни трей не занимались
    сборкой графа: правило проекта — это делает только `runtime.py`.
    """
    return build_skills_runtime(cfg or get_config(), with_audio=with_audio)


async def build_skills_runtime(
    cfg: SinapsConfig | None = None,
    *,
    with_audio: bool = True,
    audio_file: str | None = None,
    require_activation: bool | None = None,
    user_name: str = "Капитан",
) -> Runtime:
    """Собирает ассистента с пониманием команд (этап 2+).

    Отличие от `build_runtime`: поднимает платформу, реестр навыков и
    движок `SkillEngine` вместо эхо-заглушки.
    """
    from pathlib import Path

    cfg = cfg or get_config()

    bus = EventBus()
    session = SessionController(bus)
    metrics = Metrics()

    stt = build_stt(cfg)
    tts = build_tts(cfg)

    player = AudioPlayer(
        sample_rate=tts.sample_rate,
        device=cfg.audio.output_device,
        volume=cfg.tts.volume,
    )

    platform = build_platform()
    log.info("Возможности платформы: %s", platform.name)

    registry = build_default_registry()

    # --- загрузка плагинов ---
    # Файл runtime.py лежит в sinaps/, корень проекта — parents[1]
    plugins_dir = Path(__file__).resolve().parents[1] / "plugins"
    load_report: LoadReport = await load_all(plugins_dir, registry, bus=bus)
    log.info("Плагины: %s", load_report.summary())
    for folder, reason in load_report.failed:
        log.warning("Плагин %s не загружен: %s", folder, reason)

    engine = SkillEngine(
        registry,
        platform=platform,
        session=session,
        bus=bus,
        user_name=user_name,
    )

    capture = None
    listener = None
    if with_audio:
        from .audio.source import make_source

        capture = make_source(
            audio_file,
            sample_rate=cfg.audio.sample_rate,
            frame_ms=cfg.audio.frame_ms,
            pre_roll_ms=cfg.audio.pre_roll_ms,
            device=cfg.audio.input_device,
            latency=cfg.audio.latency,
        )
        if require_activation is None:
            require_activation = cfg.wake.mode == "hotkey"
        listener = ListeningPipeline(
            capture=capture, stt=stt, vad_engine=cfg.vad.engine,
            session=session, bus=bus, metrics=metrics,
            vad_threshold=cfg.vad.threshold, silence_ms=cfg.vad.silence_ms,
            min_speech_ms=cfg.vad.min_speech_ms,
            max_utterance_s=cfg.vad.max_utterance_s,
            listen_timeout_s=cfg.wake.listen_timeout_s,
            require_activation=require_activation,
            min_confidence=cfg.stt.min_confidence,
            record_audio=cfg.privacy.record_audio,
        )

    # Один объект подтверждения на оба места: конвейер должен видеть тот
    # же вопрос, который задал интерфейс. Два разных guard означали бы,
    # что на вопрос «отозвать права?» отвечает один, а действие выполняет
    # другой — и «да» прошло бы мимо.
    guard = VoiceGuard()

    conversation = ConversationPipeline(
        bus=bus, session=session, tts=tts, player=player, metrics=metrics,
        responder=engine.respond, memory=_make_memory(cfg), guard=guard,
    )
    conversation.install()

    trigger = None
    if listener is not None and require_activation:
        trigger = build_trigger(cfg, listener, loop=_current_loop())

    return Runtime(
        config=cfg, bus=bus, session=session, metrics=metrics,
        capture=capture, player=player, stt=stt, tts=tts,
        listener=listener, conversation=conversation,
        registry=registry, platform=platform, trigger=trigger,
        plugins=load_report, _plugin_guard=guard,
    )


def _overflow_reporter(bus: EventBus):
    """Сообщить в шину о потере кадров — признак, что STT не успевает."""
    last = [0.0]

    def report(dropped_total: int) -> None:
        # Колбэк приходит из потока Portaudio: здесь нельзя ни await,
        # ни получать текущий event loop — только плановая метка времени.
        now = time.monotonic()
        if now - last[0] > 5.0:
            last[0] = now
            log.warning("Аудио-очередь переполнена, потеряно %d сэмплов", dropped_total)
            _schedule_error(bus, dropped_total)

    return report


def _schedule_error(bus: EventBus, dropped: int) -> None:
    """Публикует ошибку в шину из произвольного потока."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Цикла ещё нет (идёт инициализация) — событие не критично, теряем.
        return
    loop.call_soon_threadsafe(
        lambda: asyncio.ensure_future(bus.emit(EventType.ERROR, where="capture", dropped=dropped))
    )
