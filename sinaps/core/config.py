"""Конфигурация: YAML + dataclass + валидация + hot reload.

Принципы:
  * значения по умолчанию живут в коде (`DEFAULT`), а не в файле — файл
    содержит только отличия, поэтому обновление программы не ломает настройки;
  * неизвестный ключ в файле — не ошибка, а предупреждение (пользователь
    добавил ключ для будущей версии);
  * `reload()` можно вызвать из UI, чтобы применить настройки без перезапуска.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.yaml"


@dataclass
class AudioConfig:
    sample_rate: int = 16000          # 16 кГц — требование всех STT-движков
    frame_ms: int = 20                # размер фрейма Portaudio
    pre_roll_ms: int = 500            # аудио ДО wake-слова (иначе теряем 1-е слово)
    input_device: str | int | None = None   # None — системный микрофон по умолчанию
    output_device: str | int | None = None
    peak_normalize: bool = True       # привести пики к 0.95 перед распознаванием

    # Размер буфера Portaudio: low | high | <секунды>.
    #
    # `low` — «как можно меньше», около двух фреймов, то есть ~40 мс.
    # Этого не хватает: аудиоколбэк живёт в отдельном C-потоке и должен
    # получить GIL, а главный поток может держать его долго (распознавание,
    # синтез). Тогда буфер переполняется, Portaudio выбрасывает кадры,
    # и звук рвётся.
    #
    # `high` добавляет задержку, но её никто не заметит: всё начинается
    # с pre-roll в 500 мс и ожидания конца фразы.
    # Понижай до `low`, только если переполнений нет и нужен минимум.
    latency: str = "high"


@dataclass
class VadConfig:
    engine: str = "silero"            # silero | energy  (energy — запасной, без моделей)
    threshold: float = 0.5
    # Удержание тишины перед концом фразы. 600 мс — баланс между «не обрезать
    # слово» и «не ждать вечно».
    silence_ms: int = 600
    min_speech_ms: int = 300          # короче — считаем шумом
    max_utterance_s: float = 15.0     # страховка от бесконечной записи


@dataclass
class SttConfig:
    engine: str = "faster_whisper"    # faster_whisper | vosk | none
    # Замерено на RTX 4060: large-v3-turbo даёт уверенность 0.92 против
    # 0.83 у small, и на живом микрофоне раз��ица куда заметнее, чем на
    # чистых фикстурах. small путал «блокнот» с «блокмут».
    model: str = "large-v3-turbo"    # tiny | base | small | medium | large-v3-turbo
    device: str = "auto"              # auto | cuda | cpu
    compute_type: str = "int8_float16"  # auto | int8 | float16 | int8_float16
    language: str = "ru"
    beam_size: int = 1
    # Если уверенность ниже порога и разрешён запасной движок — пробуем его.
    fallback_confidence: float = 0.0
    initial_prompt: str = ""          # подсказка-словарь для Whisper

    # --- защита от галлюцинаций -------------------------------------------
    #
    # Whisper на тишине и шуме не молчит, а сочиняет правдоподобные фразы:
    # «субтитры», «спокойная музыка», «пожалуйста, подпишитесь». Это не
    # ошибка модели, а её устройство: на входе без речи она выбирает
    # наиболее вероятное продолжение.
    #
    # Измерено на этой модели (small, CPU):
    #     живая речь          conf 0.53 - 0.92
    #     галлюцинации        conf 0.00 - 0.06
    # Разрыв десятикратный, поэтому порог берём с запасом от обеих сторон.
    #
    # Ниже порога фраза не превращается в команду, а считается пустой.
    min_confidence: float = 0.25

    # Порог громкости, ниже которого в модель идти незачем: экономит
    # 0.3-0.5 с и одновременно убирает самый частый источник выдумок.
    # Измеренный фон комнаты обычно -50...-60 dBFS, речь выше -30.
    min_dbfs: float = -50.0


@dataclass
class TtsConfig:
    engine: str = "silero"            # silero | piper | none
    voice: str = ""                  # пусто — голос по умолчанию
    speed: float = 1.0                # 0.5 … 2.0
    volume: float = 1.0               # 0.0 … 1.0
    sample_rate: int = 24000          # Silero v5 выдаёт 24 кГц
    #: Приветствовать при старте, в том числе при загрузке Windows.
    #:
    #: Полезно, когда ассистент запускается вместе с системой: иначе
    #: непонятно, работает он или просто иконка в трее. Но каждый раз
    #: при перезагрузке слышать «доброе утро» утомительно, поэтому
    #: выключается одним движением.
    greet_on_start: bool = True


@dataclass
class WakeConfig:
    """Как пользователь будит ассистента.

    Настройки принимают и сочетания клавиш (`ctrl+space`), и кнопки мыши
    (`mouse5`, `back`, `средняя`) — формат один, разбирается в
    `ui.trigger.parse_trigger`.
    """

    mode: str = "hotkey"              # hotkey | none  (wake-word появится в v1)

    # Активация: открыть окно прослушивания.
    # По умолчанию — боковая кнопка мыши «вперёд», с клавиатуры как запасной.
    hotkey: str = "ctrl+space"
    mouse_button: str = "mouse5"

    # Удержание: пока держишь — слушаем (классический push-to-talk).
    push_to_talk: str = "mouse4"

    # Окно, в котором мы готовы слушать команду после активации.
    listen_timeout_s: float = 8.0


@dataclass
class PrivacyConfig:
    local_only: bool = True           # жёсткий запрет сетевых вызовов (требование проекта)
    record_audio: bool = False        # сохранять аудио на диск (только для отладки)
    keep_history_days: int = 30


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file_level: str = "DEBUG"
    console_level: str = "INFO"
    log_file: str = "data/logs/sinaps.log"
    max_bytes: int = 2_000_000
    backup_count: int = 3
    trace_dialogues: bool = True      # отдельный лог «что сказал пользователь»


@dataclass
class SinapsConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    # --- сериализация -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SinapsConfig":
        return _from_dict(cls, data or {})

    def dumps(self) -> str:
        return yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False)

    # --- валидация --------------------------------------------------------

    def validate(self) -> list[str]:
        """Возвращает список предупреждений (не бросает — мелкие проблемы не должны
        мешать запуску). Критичные ошибки вызывают ConfigError."""
        problems: list[str] = []

        if self.audio.sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000):
            problems.append(f"audio.sample_rate={self.audio.sample_rate} — лучше 16000")
        if not 5 <= self.audio.frame_ms <= 100:
            raise ConfigError(f"audio.frame_ms={self.audio.frame_ms}: допустимо 5..100 мс")
        if self.audio.frame_ms > self.vad.silence_ms:
            problems.append("audio.frame_ms больше vad.silence_ms — границы фразы будут неточными")
        if not 0.1 <= self.vad.threshold <= 0.99:
            raise ConfigError(f"vad.threshold={self.vad.threshold}: допустимо 0.1..0.99")
        if self.stt.language not in ("ru", "en"):
            problems.append(f"stt.language={self.stt.language!r}: сейчас проверены ru/en")
        if not 0.5 <= self.tts.speed <= 2.0:
            raise ConfigError(f"tts.speed={self.tts.speed}: допустимо 0.5..2.0")
        if not 0.0 <= self.tts.volume <= 1.0:
            raise ConfigError(f"tts.volume={self.tts.volume}: допустимо 0.0..1.0")
        return problems


# --- работа с YAML ----------------------------------------------------------


def _asdict(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _asdict(getattr(obj, f.name)) for f in fields(obj)}
    return obj


def _from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Собирает dataclass из dict, молча игнорируя неизвестные ключи."""
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        f = known.get(key)
        if f is None:
            log.warning("Неизвестный ключ конфига '%s.%s' — игнорирую", cls.__name__, key)
            continue
        current = getattr(cls(), f.name, None) if is_dataclass(cls) else None
        if is_dataclass(current) and isinstance(value, dict):
            kwargs[key] = _from_dict(type(current), value)
        elif isinstance(value, dict):
            kwargs[key] = _from_dict(type(f.default), value) if f.default is not None else value
        else:
            kwargs[key] = value
    return cls(**kwargs)


# --- загрузка/сохранение ----------------------------------------------------


_lock = threading.RLock()
_current = SinapsConfig()


def load_config(path: str | os.PathLike[str] | None = None, *, autostart: bool = True) -> SinapsConfig:
    """Читает YAML (если есть) поверх значений по умолчанию."""
    global _current
    cfg_path = Path(path) if path else CONFIG_PATH

    raw: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ConfigError(f"Ожидался словарь в {cfg_path}, получено {type(raw).__name__}")
        except yaml.YAMLError as exc:
            raise ConfigError(f"Не удалось разобрать {cfg_path}: {exc}") from exc
    elif autostart:
        save_config(SinapsConfig(), cfg_path)

    cfg = SinapsConfig.from_dict(raw)
    for problem in cfg.validate():
        log.warning("Конфиг: %s", problem)

    with _lock:
        _current = cfg
    return cfg


def get_config() -> SinapsConfig:
    with _lock:
        return copy.deepcopy(_current)


def reload_config(path: str | os.PathLike[str] | None = None) -> SinapsConfig:
    log.info("Перечитываю конфигурацию")
    return load_config(path)


def save_config(cfg: SinapsConfig, path: str | os.PathLike[str] | None = None) -> Path:
    cfg_path = Path(path) if path else CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(cfg.dumps(), encoding="utf-8")
    log.debug("Конфигурация сохранена: %s", cfg_path)
    return cfg_path
