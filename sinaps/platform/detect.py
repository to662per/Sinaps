"""Выбор реализации платформы.

Проект нацелен на Windows, поэтому реализация одна, и выбирать особо нечего.
Но структура оставлена такой, чтобы добавление другой ОС позже не потребовало
переделки навыков: навыки работают только с протоколом `PlatformCapabilities`.

Никогда не бросает: если реализация не загрузилась, отдаётся заглушка, и
ассистент продолжает работать в том, что умеет (текст, время, заметки).
"""

from __future__ import annotations

import logging
import platform

from .base import PlatformCapabilities, Unsupported

log = logging.getLogger(__name__)

TARGET_OS = "windows"


def current_os() -> str:
    return platform.system().lower()  # 'windows' | 'darwin' | 'linux' | ...


def is_windows() -> bool:
    return current_os() == TARGET_OS


def build_platform() -> PlatformCapabilities:
    """Реализация возможностей для текущей ОС. Без исключений наверх."""
    if is_windows():
        try:
            from .windows import WindowsPlatform

            log.info("Платформа: Windows")
            return WindowsPlatform()
        except Exception:
            # Нет доступа к Win32 API или сломан ctypes — не повод ронять
            # ассистента целиком: работаем в ограниченном режиме.
            log.exception("Не удалось загрузить WindowsPlatform")

    log.warning(
        "Платформа '%s' пока не поддерживается (целевая — Windows). "
        "Доступен ограниченный режим: текстовый ввод и системные функции.",
        current_os(),
    )
    return Unsupported()
