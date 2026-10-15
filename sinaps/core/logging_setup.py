"""Логирование: консоль + ротируемый файл + отдельный трек диалогов.

Три потока логов, потому что у них разная судьба:
  * консоль  — для пользователя, INFO, без шума;
  * файл    — для отладки, DEBUG, ротируется, чтобы не съесть диск;
  * диалоги — только «пользователь сказал / ассистент ответил / ошибка»,
    в отдельном файле. Это то, что нужно для разбора «ассистент не понял».
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .bus import Event, EventType
from .config import PROJECT_ROOT, SinapsConfig, get_config

_configured = False
_dialogue_log: logging.Logger | None = None

# Модули, которые шумят на уровне DEBUG и заслоняют полезные сообщения.
_NOISY = ("numba", "urllib3", "filelock", "requests", "jieba", "jieba_fast", "matplotlib")


def setup_logging(cfg: SinapsConfig | None = None) -> None:
    global _configured, _dialogue_log
    if _configured:
        return

    cfg = cfg or get_config()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # фильтры ниже всё равно отсеют лишнее

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
        datefmt="%H:%M:%S",
    )

    # --- консоль ---
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, cfg.logging.console_level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    # --- файл с ротацией ---
    log_path = PROJECT_ROOT / cfg.logging.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=cfg.logging.max_bytes,
        backupCount=cfg.logging.backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, cfg.logging.file_level.upper(), logging.DEBUG))
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # --- диалоги (отдельный, компактный, легко читается) ---
    dialogue_path = log_path.parent / "dialogues.log"
    _dialogue_log = logging.getLogger("sinaps.dialogues")
    _dialogue_log.propagate = False
    if cfg.logging.trace_dialogues:
        dh = logging.handlers.RotatingFileHandler(
            dialogue_path, maxBytes=cfg.logging.max_bytes,
            backupCount=2, encoding="utf-8",
        )
        dh.setLevel(logging.INFO)
        dh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
        _dialogue_log.addHandler(dh)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    _silence_third_party_warnings()

    _configured = True


def _silence_third_party_warnings() -> None:
    """Глушит безобидные, но громкие предупреждения сторонних библиотек.

    Не трогаем наши собственные предупреждения — только те, что приходят из
    torch: они печатаются в stderr мимо нашей системы логов и заслоняют
    реальный вывод ассистента.
    """
    import warnings

    for pattern in (
        r".*TypedStorage is deprecated.*",
        r".*torch\.jit.*is deprecated.*",
        r".*Please switch to `torch\.export`.*",
    ):
        warnings.filterwarnings("ignore", message=pattern)


def get_dialogue_logger() -> logging.Logger:
    """Логгер диалогов. Безопасно вызывать до `setup_logging`."""
    global _dialogue_log
    if _dialogue_log is None:
        _dialogue_log = logging.getLogger("sinaps.dialogues")
    return _dialogue_log


# --- хелперы ----------------------------------------------------------------


def log_user(text: str) -> None:
    text = (text or "").strip()
    if text:
        get_dialogue_logger().info("ПОЛЬЗОВАТЕЛЬ: %s", text)


def log_assistant(text: str) -> None:
    text = (text or "").strip()
    if text:
        get_dialogue_logger().info("СИНАПС:   %s", text)


def log_dialogue_error(msg: str) -> None:
    get_dialogue_logger().error("ОШИБКА:   %s", msg)


def log_dialogue_guard(msg: str) -> None:
    """Ответ на подтверждение голосом.

    Отдельной строкой, а не через `log_assistant`: в журнале разговора
    видно, что человек ответил «да» на отзыв прав, а не просто сказал
    «да» в пустоту. По общему виду реплик это не различить.
    """
    msg = (msg or "").strip()
    if msg:
        get_dialogue_logger().info("ПОДТВ.:   %s", msg)


def trace_event(event: Event) -> None:
    """Лёгкий след событий — только в файл, не в консоль."""
    logging.getLogger("sinaps.events").debug("%s %s", event.type.value, event.payload)


def install_event_tracing(bus) -> None:
    if not _configured:
        return
    log = logging.getLogger("sinaps.events")

    async def _trace(event: Event) -> None:
        if event.type in (EventType.AUDIO_STARTED, EventType.SKILL_STARTED):
            return
        log.debug("%s %s", event.type.value, event.payload)

    bus.on(*EventType)(_trace)
