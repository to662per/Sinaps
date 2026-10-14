"""Общие фикстуры тестов.

Заглушка платформы живёт здесь, а не в конкретном тест-модуле: её используют
несколько наборов тестов, а импорт `tests.xxx` без `tests/__init__.py`
приводит к неоднозначности и зависаниям при совместном запуске.
"""

from __future__ import annotations

import pytest

from sinaps.platform.base import (
    AppInfo,
    PowerAction,
    SystemStatus,
    TextSelection,
    VolumeState,
    WindowInfo,
)


class FakePlatform:
    """Заглушка возможностей ОС: записывает вызовы и ничего не делает."""

    name = "fake"
    supported_power = (
        PowerAction.SHUTDOWN,
        PowerAction.RESTART,
        PowerAction.SLEEP,
        PowerAction.LOCK,
        PowerAction.SIGN_OUT,
    )

    def __init__(self, volume_percent: int = 50) -> None:
        self.calls: list[str] = []
        #: Уровень громкости меняется по-настоящему: навык читает его
        #: до и после сдвига и отвечает «теперь N процентов». Заглушка,
        #: которая всегда возвращает одно и то же число, проверяла бы
        #: саму себя, а не код.
        self.volume_percent = volume_percent

    def launch_app(self, name: str) -> tuple[bool, str]:
        self.calls.append(f"launch:{name}")
        return (True, "ok") if "нету" not in name else (False, "не найдено")

    def list_apps(self) -> list[AppInfo]:
        return [AppInfo(name="Блокнот", target="notepad.exe")]

    def volume_up(self, percent: int = 10) -> bool:
        self.calls.append(f"volume_up:{percent}")
        self.volume_percent = min(100, self.volume_percent + percent)
        return True

    def volume_down(self, percent: int = 10) -> bool:
        self.calls.append(f"volume_down:{percent}")
        self.volume_percent = max(0, self.volume_percent - percent)
        return True

    def volume_mute(self) -> bool:
        self.calls.append("mute")
        return True

    def volume_state(self) -> VolumeState:
        # Держим настоящий процент, а не None: навык громкости читает
        # уровень до и после изменения, иначе нечего проверять.
        return VolumeState(percent=self.volume_percent, supported=True)

    def media_play_pause(self) -> bool:
        self.calls.append("play_pause")
        return True

    def media_next(self) -> bool:
        self.calls.append("next")
        return True

    def media_previous(self) -> bool:
        self.calls.append("prev")
        return True

    def list_windows(self) -> list[WindowInfo]:
        return [WindowInfo(handle=42, title="Блокнот", process="notepad.exe")]

    def focus_window(self, handle: int) -> bool:
        self.calls.append(f"focus:{handle}")
        return True

    def close_window(self, handle: int) -> bool:
        self.calls.append(f"close_window:{handle}")
        return True

    def resolve_process_name(self, query: str) -> str:
        # Та же таблица, что и на настоящей Windows-платформе: иначе
        # тест проверял бы заглушку вместо кода.
        return {
            "блокнот": "notepad",
            "notepad": "notepad",
            "калькулятор": "calc",
            "кальк": "calc",
            "диспетчер задач": "taskmgr",
            "paint": "mspaint",
            "проводник": "explorer",
            "командная строка": "cmd",
        }.get(query.strip().lower(), "")

    def minimize_all(self) -> bool:
        self.calls.append("minimize_all")
        return True

    def type_text(self, text: str) -> bool:
        self.calls.append(f"type:{text}")
        return True

    def clipboard_get(self) -> TextSelection:
        return TextSelection(text="скопировано", available=True)

    def clipboard_set(self, text: str) -> bool:
        self.calls.append(f"clipboard_set:{text}")
        return True

    def screenshot(self, path=None) -> str:
        self.calls.append("screenshot")
        return "C:/shot.png"

    def set_brightness(self, percent: int) -> bool:
        self.calls.append(f"brightness:{percent}")
        return False

    def status(self) -> SystemStatus:
        return SystemStatus(
            cpu_percent=23.0, memory_percent=61.0,
            memory_used_gb=9.8, memory_total_gb=16.0,
            disk_percent=71.0, disk_free_gb=88.4,
            battery_percent=87.0, plugged=True, uptime_hours=51.3,
        )

    def open_path(self, path: str, *, reveal: bool = False) -> bool:
        self.calls.append(f"open_path:{path}:{reveal}")
        return True

    def power_action(self, action: PowerAction, *, delay_s: int = 0) -> bool:
        self.calls.append(f"power:{action.value}:{delay_s}")
        return True


@pytest.fixture
def platform_stub() -> FakePlatform:
    """Свежая заглушка на каждый тест: список вызовов не протекает."""
    return FakePlatform()
