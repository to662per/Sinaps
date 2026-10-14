"""Возможности операционной системы.

Это единственный слой, который знает про Windows/macOS/Linux. Навыки работают
только с этими интерфейсами и никогда — с конкретной ОС. Благодаря этому
навык «открыть приложение» один и тот же на всех платформах, а различия
живут в одном файле на каждую ОС.

Правило: если ты добавляешь новую возможность, сначала описываешь её здесь
как протокол, потом реализуешь в `windows.py`/`macos.py`/`linux.py`, и только
потом пишешь навык. Иначе через месяц навык будет знать про `pywin32`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


# --- типы данных ------------------------------------------------------------


class AppLaunchMethod(str, enum.Enum):
    EXECUTABLE = "executable"      # прямой путь к .exe
    URI = "uri"                    # ms-settings:, mailto:, https:
    SHORTCUT = "shortcut"          # .lnk в меню «Пуск»
    SHELL = "shell"                # системная команда / ассоциация


class PowerAction(str, enum.Enum):
    SHUTDOWN = "shutdown"
    RESTART = "restart"
    SLEEP = "sleep"
    LOCK = "lock"
    SIGN_OUT = "sign_out"


@dataclass(frozen=True)
class AppInfo:
    """Приложение, найденное в системе."""

    name: str                    # как звать голосом
    target: str                  # путь, который открываем
    method: AppLaunchMethod = AppLaunchMethod.EXECUTABLE
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class VolumeState:
    """Громкость. `percent` = None, если система не умеет её читать."""

    percent: int | None = None
    muted: bool = False
    supported: bool = True


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process: str = ""
    visible: bool = True


@dataclass(frozen=True)
class SystemStatus:
    cpu_percent: float | None = None
    memory_percent: float | None = None
    memory_used_gb: float | None = None
    memory_total_gb: float | None = None
    disk_percent: float | None = None
    disk_free_gb: float | None = None
    battery_percent: float | None = None
    plugged: bool | None = None
    uptime_hours: float | None = None


@dataclass
class TextSelection:
    """Результат чтения буфера обмена."""

    text: str = ""
    available: bool = False
    reason: str = ""


# --- протоколы возможностей --------------------------------------------------


@runtime_checkable
class VolumeSupport(Protocol):
    """Громкость. Единица во всех методах — проценты, не нажатия.

    Раньше параметр назывался `steps` и означал число нажатий клавиши.
    Одно нажатие сдвигает уровень на 2 процента, поэтому «5 ступеней»
    на слух и на экране означали разное: голос говорил 5, а громкость
    уезжала на 10. Проценты — это то, что человек видит и о чём
    говорит, поэтому единица измерения вынесена в имя параметра.
    """

    def volume_up(self, percent: int = 10) -> bool: ...
    def volume_down(self, percent: int = 10) -> bool: ...
    def volume_mute(self) -> bool: ...
    def volume_state(self) -> VolumeState: ...


@runtime_checkable
class MediaSupport(Protocol):
    def media_play_pause(self) -> bool: ...
    def media_next(self) -> bool: ...
    def media_previous(self) -> bool: ...


@runtime_checkable
class WindowSupport(Protocol):
    def list_windows(self) -> list[WindowInfo]: ...
    def focus_window(self, handle: int) -> bool: ...
    def close_window(self, handle: int) -> bool: ...
    def minimize_all(self) -> bool: ...


@runtime_checkable
class PowerSupport(Protocol):
    def power_action(self, action: PowerAction, *, delay_s: int = 0) -> bool: ...


@runtime_checkable
class SystemSupport(Protocol):
    def status(self) -> SystemStatus: ...
    def open_path(self, path: str, *, reveal: bool = False) -> bool: ...


@runtime_checkable
class ScreenSupport(Protocol):
    def screenshot(self, path: str | None = None) -> str: ...
    def set_brightness(self, percent: int) -> bool: ...


@runtime_checkable
class InputSupport(Protocol):
    def type_text(self, text: str) -> bool: ...
    def clipboard_get(self) -> TextSelection: ...
    def clipboard_set(self, text: str) -> bool: ...


@runtime_checkable
class PlatformCapabilities(Protocol):
    """Всё, что умеет текущая ОС. Отсутствующее = заглушка с понятной ошибкой."""

    name: str
    supported_power: tuple[PowerAction, ...]

    # Приложения
    def launch_app(self, name: str) -> tuple[bool, str]: ...
    def list_apps(self) -> list[AppInfo]: ...

    # Звук и медиа
    def volume_up(self, steps: int = 1) -> bool: ...
    def volume_down(self, steps: int = 1) -> bool: ...
    def volume_mute(self) -> bool: ...
    def volume_state(self) -> VolumeState: ...
    def media_play_pause(self) -> bool: ...
    def media_next(self) -> bool: ...
    def media_previous(self) -> bool: ...

    # Окна
    def list_windows(self) -> list[WindowInfo]: ...
    def focus_window(self, handle: int) -> bool: ...
    def close_window(self, handle: int) -> bool: ...
    def minimize_all(self) -> bool: ...

    # Ввод и буфер
    def type_text(self, text: str) -> bool: ...
    def clipboard_get(self) -> TextSelection: ...
    def clipboard_set(self, text: str) -> bool: ...

    # Экран
    def screenshot(self, path: str | None = None) -> str: ...
    def set_brightness(self, percent: int) -> bool: ...

    # Система
    def status(self) -> SystemStatus: ...
    def open_path(self, path: str, *, reveal: bool = False) -> bool: ...
    def power_action(self, action: PowerAction, *, delay_s: int = 0) -> bool: ...


# --- заглушки ----------------------------------------------------------------


class Unsupported(PlatformCapabilities):
    """Заглушка для возможностей, которых нет на этой ОС.

    Важно: лучше честно сказать «не умею», чем молча сделать вид, что
    получилось. Навык должен получать внятную ошибку и озвучить её.
    """

    name = "unsupported"
    supported_power: tuple[PowerAction, ...] = ()

    def _no(self, what: str) -> bool:
        from ..core.logging_setup import get_dialogue_logger

        get_dialogue_logger().info("НЕ ПОДДЕРЖИВАЕТСЯ: %s", what)
        return False

    def launch_app(self, name: str) -> tuple[bool, str]:
        return False, f"Запуск приложений не поддерживается на {self.name}"

    def list_apps(self) -> list[AppInfo]:
        return []

    def volume_up(self, percent: int = 10) -> bool:
        return self._no("volume_up")

    def volume_down(self, percent: int = 10) -> bool:
        return self._no("volume_down")

    def volume_mute(self) -> bool:
        return self._no("volume_mute")

    def volume_state(self) -> VolumeState:
        return VolumeState(supported=False)

    def media_play_pause(self) -> bool:
        return self._no("media_play_pause")

    def media_next(self) -> bool:
        return self._no("media_next")

    def media_previous(self) -> bool:
        return self._no("media_previous")

    def list_windows(self) -> list[WindowInfo]:
        return []

    def focus_window(self, handle: int) -> bool:
        return self._no("focus_window")

    def close_window(self, handle: int) -> bool:
        return self._no("close_window")

    def minimize_all(self) -> bool:
        return self._no("minimize_all")

    def type_text(self, text: str) -> bool:
        return self._no("type_text")

    def clipboard_get(self) -> TextSelection:
        return TextSelection(available=False, reason="буфер обмена недоступен")

    def clipboard_set(self, text: str) -> bool:
        return self._no("clipboard_set")

    def screenshot(self, path: str | None = None) -> str:
        raise NotImplementedError("Скриншоты не поддерживаются")

    def set_brightness(self, percent: int) -> bool:
        return self._no("set_brightness")

    def status(self) -> SystemStatus:
        return SystemStatus()

    def open_path(self, path: str, *, reveal: bool = False) -> bool:
        return self._no("open_path")

    def power_action(self, action: PowerAction, *, delay_s: int = 0) -> bool:
        return self._no(f"power_action({action})")


@dataclass
class UnsupportedRegistry:
    """Набор заглушек, чтобы навык всегда получал объект, а не None.

    `None` в полях — сигнал для разработчика, но не для навыка: он всё
    равно может вызвать метод и получить внятный отказ.
    """

    volume: Unsupported = field(default_factory=Unsupported)
    media: Unsupported = field(default_factory=Unsupported)
    windows: Unsupported = field(default_factory=Unsupported)
    input: Unsupported = field(default_factory=Unsupported)
    screen: Unsupported = field(default_factory=Unsupported)
    power: Unsupported = field(default_factory=Unsupported)
    system: Unsupported = field(default_factory=Unsupported)
