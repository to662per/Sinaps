"""Контейнер зависимостей и запуск ядра.

Собирает все движки из конфига в один граф и передаёт его конвейерам.
Отдельная функция `build_runtime` существует ради тестов: она позволяет
подсунуть заглушки вместо микрофона и моделей.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

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
from .plugins.loader import load_all, LoadReport
from .stt.base import STTEngine, load_engine as load_stt
from .tts.base import TTSEngine, load_engine as load_tts
from .ui.trigger import ActivationTrigger, Trigger, parse_trigger

log = logging.getLogger(__name__)


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
        if self.listener is not None:
            await self.listener.stop()
        if self.trigger is not None:
            self.trigger.stop()
        self.player.cancel()

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
            f"  Сессия   : {self.session.state.value}"
        )

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

    conversation = ConversationPipeline(
        bus=bus,
        session=session,
        tts=tts,
        player=player,
        metrics=metrics,
        responder=responder,
    )
    conversation.install()

    trigger = None
    if listener is not None and require_activation:
        trigger = build_trigger(cfg, listener, loop=_current_loop())

    return Runtime(
        config=cfg, bus=bus, session=session, metrics=metrics,
        capture=capture, player=player, stt=stt, tts=tts,
        listener=listener, conversation=conversation,
        trigger=trigger,
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
    load_report: LoadReport = await load_all(plugins_dir, registry)
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

    conversation = ConversationPipeline(
        bus=bus, session=session, tts=tts, player=player, metrics=metrics,
        responder=engine.respond, memory=_make_memory(cfg),
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
