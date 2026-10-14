"""Обвязка для проверки плагинов без живого ассистента.

Зачем она
---------
Плагин без возможности проверить его на живой машине — это плагин,
который пишут и запускают первый раз на пользователе. Обвязка даёт
автору две вещи, которых иначе нет:

  * `FakePlatform` — возможности ОС, которые записывают вызовы и
    ничего не делают. Настоящий ПК для этого не нужен и быть не
    должен: тест, который включается и выключает компьютер, хуже
    теста, который его не трогает.
  * `run_phrase` — прогнать фразу через настоящий реестр и получить
    `Response`. Сопоставление и вызов идут через `SkillRegistry`,
    то есть ровно тем же кодом, что и в ассистенте. Обвязка не имеет
    своей логики разбора — иначе тест проверял бы её, а не плагин.

Про заглушку платформы отдельно
--------------------------------
`FakePlatform` раньше жил в `tests/conftest.py` и был виден только
внутри набора тестов самого проекта. Автору плагина он был недоступен,
хотя это ровно тот инструмент, который ему нужен.

Он не попал в `sinaps/plugins/api.py` намеренно: `api.py` — контракт
работающего плагина, и если туда положить заглушку, у плагина появится
причина тянуть её в production. Обвязка публична, но живёт отдельно.

Про изоляцию одобрений
----------------------
Плагину может понадобиться право, а боевой `data/plugins.json`
трогать в тестах нельзя. Поэтому `grant` пишет одобрение во временный
файл и возвращает настоящий обратно на место. Тест не может по ошибке
выдать самому себе доступ к сети и потом забыть об этом.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Sequence

from ..core.context import DialogContext
from ..core.errors import SinapsError
from ..platform.base import (AppInfo, PowerAction, SystemStatus, TextSelection,
                             VolumeState, WindowInfo)
from ..skills.base import Response
from ..skills.registry import SkillRegistry


class PhraseNotMatched(SinapsError):
    """Фраза не попала ни в одну команду.

    Отдельный тип, а не пустой ответ: тест, который молча получил
    «не понял», проходит, и автор узнаёт о промахе только на живом
    ассистенте — когда команда уже отвечает пользователю.
    """


class FakePlatform:
    """Возможности ОС, которые записывают вызовы и ничего не делают."""

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
        #: всегда возвращающая одно и то же число, проверяла бы саму
        #: себя, а не код.
        self.volume_percent = volume_percent
        self.clipboard = TextSelection(text="скопировано", available=True)

    # --- приложения --------------------------------------------------------

    def launch_app(self, name: str) -> tuple[bool, str]:
        self.calls.append(f"launch:{name}")
        return (True, "ok") if "нету" not in name else (False, "не найдено")

    def list_apps(self) -> list[AppInfo]:
        return [AppInfo(name="Блокнот", target="notepad.exe")]

    # --- звук --------------------------------------------------------------

    def volume_up(self, percent: int = 10) -> bool:
        self.calls.append(f"volume_up:{percent}")
        self.volume_percent = min(100, self.volume_percent + percent)
        return True

    def volume_down(self, percent: int = 10) -> bool:
        self.calls.append(f"volume_down:{percent}")
        self.volume_percent = max(0, self.volume_percent - percent)
        return True

    def volume_set(self, percent: int) -> bool:
        self.calls.append(f"volume_set:{percent}")
        self.volume_percent = max(0, min(100, int(percent)))
        return True

    def volume_mute(self) -> bool:
        self.calls.append("mute")
        return True

    def volume_state(self) -> VolumeState:
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

    # --- окна --------------------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        return [WindowInfo(handle=42, title="Блокнот", process="notepad.exe")]

    def focus_window(self, handle: int) -> bool:
        self.calls.append(f"focus:{handle}")
        return True

    def close_window(self, handle: int) -> bool:
        self.calls.append(f"close_window:{handle}")
        return True

    def minimize_all(self) -> bool:
        self.calls.append("minimize_all")
        return True

    def resolve_process_name(self, query: str) -> str:
        # Та же таблица, что и на настоящей Windows-платформе: иначе тест
        # проверял бы заглушку вместо кода.
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

    # --- ввод и буфер ------------------------------------------------------

    def type_text(self, text: str) -> bool:
        self.calls.append(f"type:{text}")
        return True

    def clipboard_get(self) -> TextSelection:
        return self.clipboard

    def clipboard_set(self, text: str) -> bool:
        self.calls.append(f"clipboard_set:{text}")
        self.clipboard = TextSelection(text=text, available=True)
        return True

    def screenshot(self, path=None) -> str:
        self.calls.append("screenshot")
        return "C:/shot.png"

    def set_brightness(self, percent: int) -> bool:
        self.calls.append(f"brightness:{percent}")
        return False

    # --- система ------------------------------------------------------------

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


def make_ctx(text: str = "", *, platform: FakePlatform | None = None,
             user_name: str = "Капитан",
             history: list[dict[str, str]] | None = None) -> DialogContext:
    """Контекст для прогона фразы."""
    return DialogContext(
        text=text,
        source="text",
        confidence=1.0,
        history=list(history or []),
        platform=platform,
        user_name=user_name,
    )


async def run_phrase(registry: SkillRegistry, text: str, *,
                     original: str | None = None,
                     platform: FakePlatform | None = None,
                     user_name: str = "Капитан") -> Response:
    """Прогнать фразу через реестр и вернуть ответ.

    Сопоставление и вызов — через `SkillRegistry`, тем же кодом, что и в
    ассистенте. Обвязка ничего не разбирает сама: свой разбор означал
    бы, что тест проверяет обвязку.

    Ненайденная фраза бросает `PhraseNotMatched`, а не возвращает пустой
    ответ: молчаливый «не понял» прошёл бы тест, и промах всплыл бы
    только на живой машине.
    """
    match = registry.match(text, original)
    if match is None:
        raise PhraseNotMatched(
            f"фраза «{text}» не подошла ни к одной команде. "
            f"Команды: {sorted(registry.spec_names())}"
        )
    ctx = make_ctx(text, platform=platform, user_name=user_name)
    return await registry.execute(match, ctx)


@asynccontextmanager
async def plugin_under_test(folder: str | Path,
                           grant: Sequence[str] = ()
                           ) -> AsyncIterator[SkillRegistry]:
    """Загрузить плагина в чистый реестр и убрать за собой.

    Права из `grant` выдаются во **временный** файл одобрений, боевой
    не трогается. Это не перестраховка: тест, выдавший самому себе
    доступ к сети, останется с ним, и следующий запуск проверит уже не
    то.

    Процесс плагина закрывается на выходе. Ребёнок стоит около 30 МБ,
    и забытый процесс в наборе из сотни тестов — это десятки тестов,
    которые встанут колом не из-за своей ошибки.
    """
    from . import approvals
    from . import state as state_mod
    from .loader import load_plugin

    workdir = Path(tempfile.mkdtemp(prefix="sinaps-plugin-test-"))

    real_approvals = approvals.path
    real_state = state_mod.default_path
    approvals.path = lambda: workdir / "plugins.json"
    state_mod.default_path = lambda: workdir / "plugins.db"

    if grant:
        name = _manifest_name(Path(folder) / "manifest.yaml")
        approvals.grant(name, tuple(grant))

    registry = SkillRegistry()
    try:
        plugin = await load_plugin(folder, registry)
        try:
            yield registry
        finally:
            await plugin.stop()
    finally:
        approvals.path = real_approvals
        state_mod.default_path = real_state
        shutil.rmtree(workdir, ignore_errors=True)


def _manifest_name(path: Path) -> str:
    """Имя плагина из манифеста — чтобы выдать право тому, кого грузим."""
    import re

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return path.parent.name
    found = re.search(r"^name:\s*(\S+)\s*$", text, re.MULTILINE)
    return found.group(1) if found else path.parent.name