"""Загрузка плагинов: папка с манифестом превращается в команды.

Правило номер один
------------------
**Код плагина не выполняется в процессе ассистента.** Не «мы стараемся
не исполнять», а буквально: импорт модуля и вызов `register` происходят
в дочернем процессе, а ядро получает описание команд и строит свои
`IntentSpec` само. Обработчик — тоже не чужой код, а вызов через
границу.

Почему именно так
-----------------
Разрешение `network.http`, выданное плагину в том же процессе, было бы
неисполняемым обещанием: `enforce_offline()` выставляет
`HF_HUB_OFFLINE`, но `urllib.request` этих переменных не читает.
Импортировал — и пошёл. Значит право надо не записывать, а **выдать
возможность**: закрыть сокеты в чужом процессе так, чтобы плагин не
мог их открыть в принципе.

Что должно выжить при любой поломке
-----------------------------------
* ядро и его собственные команды;
* другие плагины;
* уже загруженная часть сломанного плагина — тоже: если `register`
  упал после трёх команд из пяти, ни одна не должна остаться в реестре.

Откат делает ребёнок, а не ядро: в его реестре снимается всё, и на
наружу уходит один отказ с понятной причиной.

Про порядок загрузки
---------------------
Плагины грузятся по алфавиту имён папок, а не по порядку каталога.
Иначе при добавлении одного файла поменялось бы то, какой из двух
плагинов с одинаковым именем выиграет, — а это трудно заметить и
невозможно воспроизвести.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..skills.base import IntentSpec, Response, Skill, SlotSpec
from ..skills.registry import SkillRegistry
from .manifest import (DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S, Manifest,
                       ManifestError, load_manifest)

log = logging.getLogger("sinaps.plugins")

#: Где ищем плагины по умолчанию: рядом с кодом, как `tools/`.
PLUGINS_DIRNAME = "plugins"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Сколько процесс молчит перед тем, как его закрыть.
#:
#: Ребёнок стоит 27 МБ. По одному на каждый плагин при десятке плагинов
#: это 270 МБ, которые никто не просил. Замерено на живой машине, а не
#: оценено. Простой закрывает процесс, следующая команда поднимает его
#: заново за четверть секунды.
IDLE_TIMEOUT_S = 120


@dataclass
class LoadedPlugin:
    """Плагин, который загрузился и работает."""

    manifest: Manifest
    process: "PluginProcess | None" = None
    commands: tuple[str, ...] = ()
    #: Права, которые реально выданы, а не только объявлены.
    granted: tuple[str, ...] = ()
    #: Права, которых не хватает: плагин загрузился, но работает не всем.
    withheld: tuple[str, ...] = ()
    network_sealed: bool = True

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def permissions(self) -> tuple[str, ...]:
        return self.manifest.permissions

    async def stop(self) -> None:
        if self.process is not None:
            await self.process.stop()


@dataclass
class LoadReport:
    """Что произошло при попытке загрузить плагины.

    Отчёт, а не исключение: часть плагинов в порядке, часть с ошибкой —
    это нормальное состояние, а не катастрофа. Исключение тут было бы
    тем же, что и «падает ядро, потому что чужая опечатка».
    """

    loaded: list[LoadedPlugin] = field(default_factory=list)
    #: Имя папки и причина. Порядок соответствует обходу.
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.loaded)

    def summary(self) -> str:
        parts = []
        if self.loaded:
            names = ", ".join(plugin.name for plugin in self.loaded)
            parts.append(f"загружено {len(self.loaded)}: {names}")
        if self.failed:
            parts.append(f"с ошибкой {len(self.failed)}")
        return "; ".join(parts) or "плагинов не найдено"

    async def stop_all(self) -> None:
        """Закрыть все дочерние процессы. Вызывается при остановке."""
        for plugin in self.loaded:
            try:
                await plugin.stop()
            except Exception:  # noqa: BLE001 — процесс уже мог умереть
                pass


def discover(plugins_dir: str | Path) -> list[Path]:
    """Найти папки с манифестом.

    Порядок по имени папки: иначе добавление одного плагина изменит то,
    какой из двух одноимённых загрузится первым.
    """
    root = Path(plugins_dir)
    if not root.exists():
        return []

    found = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_dir() and (entry / "manifest.yaml").exists():
            found.append(entry)
    return found


class PluginProcess:
    """Живой дочерний процесс одного плагина.

    Общается с ядром кадрами JSON через stdin/stdout. Не через сокет:
    `socket` запрещён в самом пакете, и это к лучшему — трубы не оставляют
    плагину сетевой привилегии в ничего не стоящем смысле.
    """

    def __init__(self, folder: Path, manifest: Manifest,
                 granted: Sequence[str] = ()) -> None:
        self.folder = folder
        self.manifest = manifest
        self.granted = tuple(granted)
        self._proc: Any = None
        self._next_id = 0
        self._last_used = 0.0
        self.ready: dict[str, Any] = {}

    # --- жизненный цикл ----------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def idle_for(self) -> float:
        if self._proc is None:
            return 0.0
        return asyncio.get_running_loop().time() - self._last_used

    async def start(self) -> dict[str, Any]:
        """Поднять процесс и получить описание команд."""
        if self.alive and self.ready:
            return self.ready

        from .protocol import (OP_HELLO, OP_READY, ProtocolError,
                               read_message_async, write_message_async)

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        log.info("Запуск плагина %s (%s)", self.manifest.name, self.folder.name)
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "sinaps.plugins.runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # stderr наследуется намеренно: трассировка чужого кода
            # должна быть видна, а не заперта в трубе, которую никто
            # не читает. Наследование не создаёт риска переполнения —
            # буфера нет, писать может кто угодно.
            stderr=None,
            cwd=str(PROJECT_ROOT),
            env=env,
        )

        try:
            await write_message_async(self._proc.stdin, {
                "op": OP_HELLO,
                "plugin": self.manifest.name,
                "entry": self.manifest.entry,
                "module": self.manifest.module,
                "function": self.manifest.function,
                "folder": str(self.folder),
                "permissions": list(self.granted),
            })
            message = await asyncio.wait_for(
                read_message_async(self._proc.stdout), MAX_TIMEOUT_S)
        except (asyncio.TimeoutError, ProtocolError, asyncio.IncompleteReadError) as exc:
            await self.stop()
            raise ManifestError(
                f"{self.folder}: плагин не ответил — {type(exc).__name__}: {exc}"
            ) from exc

        if message.get("op") == OP_READY:
            self.ready = message
            self._last_used = asyncio.get_running_loop().time()
            if not message.get("network_sealed", True) and \
                    "network.http" not in self.granted:
                log.error(
                    "Плагин %s: сеть закрыть не удалось, запрет не держит",
                    self.manifest.name)
            return message

        await self.stop()
        raise ManifestError(
            f"{self.folder}: {message.get('message', 'плагин не загрузился')}"
        )

    async def call(self, command: str, ctx: dict[str, Any],
                   slots: dict[str, Any] | None = None,
                   timeout: int = DEFAULT_TIMEOUT_S) -> Response:
        """Вызвать команду плагина и вернуть ответ.

        Таймаут здесь не «на всякий случай»: без него зависший плагин
        держит голосовой диалог открытым навсегда, и единственный
        способ вернуть систему к жизни — убить ассистента вручную.
        По истечении времени процесс закрывается целиком, следующий
        вызов поднимет новый.
        """
        from .protocol import OP_RESULT, ProtocolError
        from .protocol import read_message_async, write_message_async

        await self.start()
        assert self._proc is not None

        self._next_id += 1
        request_id = self._next_id

        await write_message_async(self._proc.stdin, {
            "op": "call",
            "id": request_id,
            "command": command,
            "ctx": ctx,
            "slots": slots or {},
        })

        try:
            message = await asyncio.wait_for(
                read_message_async(self._proc.stdout), timeout)
        except asyncio.TimeoutError as exc:
            await self.stop()
            from ..core.errors import PluginError

            raise PluginError(
                f"Плагин «{self.manifest.name}» не ответил за {timeout} с. "
                "Процесс закрыт, плагин будет запущен заново.",
                speech=f"Плагин «{self.manifest.name}» завис и был остановлен",
            ) from exc
        except (ProtocolError, asyncio.IncompleteReadError) as exc:
            await self.stop()
            from ..core.errors import PluginError

            raise PluginError(
                f"Плагин «{self.manifest.name}»: связь нарушена — {exc}",
                speech=f"Плагин «{self.manifest.name}» оборвался",
            ) from exc

        self._last_used = asyncio.get_running_loop().time()

        if message.get("id") != request_id:
            await self.stop()
            from ..core.errors import PluginError

            raise PluginError(
                f"Плагин «{self.manifest.name}»: пришёл ответ не на тот вызов",
                speech="Что-то пошло не так с плагином")

        if message.get("op") == OP_RESULT:
            return _response_from(message.get("response") or {})

        from ..core.errors import PluginError

        raise PluginError(
            f"Плагин «{self.manifest.name}»: "
            f"{message.get('message', 'неизвестная ошибка')}",
            speech=message.get("speech")
            or f"Плагин «{self.manifest.name}» не сработал")

    async def stop(self) -> None:
        """Закрыть процесс. Молчаливый провал здесь — норма."""
        from .protocol import OP_STOP, ProtocolError, write_message_async

        proc, self._proc = self._proc, None
        self.ready = {}
        if proc is None or proc.returncode is not None:
            return

        try:
            await asyncio.wait_for(
                write_message_async(proc.stdin, {"op": OP_STOP}), 5)
        except (asyncio.TimeoutError, ConnectionError, BrokenPipeError,
                AttributeError, ProtocolError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            # Вежливое завершение не сработало — убиваем насильно.
            # Плагин может игнорировать и обычный выход.
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass


def _response_from(data: dict[str, Any]) -> Response:
    known = {field: data[field] for field in
             ("speech", "display", "follow_up", "ask_for")
             if data.get(field) is not None}
    meta = data.get("meta")
    return Response(
        **known,
        show_panel=bool(data.get("show_panel", False)),
        meta=meta if isinstance(meta, dict) else {},
    )


def _slots_from(data: dict[str, Any] | None) -> dict[str, SlotSpec]:
    if not isinstance(data, dict):
        return {}
    return {
        name: SlotSpec(
            name=raw.get("name", name),
            type=raw.get("type", "string"),
            required=bool(raw.get("required", True)),
            default=raw.get("default"),
            description=raw.get("description", ""),
            min_length=int(raw.get("min_length", 1)),
        )
        for name, raw in data.items()
        if isinstance(raw, dict)
    }


def _make_handler(process: PluginProcess, command: str, plugin_name: str,
                  timeout: int):
    """Обработчик, который пересекает границу процесса.

    Имя намерения в ядре и в ребёнке различается: ребёнок знает только
    свои команды, ядро добавляет префикс плагина. Поэтому команда для
    отправки запоминается здесь, а не берётся из спецификации.
    """

    async def handler(ctx, slots) -> Response:
        payload = {
            "text": getattr(ctx, "text", "") or "",
            "source": getattr(ctx, "source", "voice"),
            "confidence": getattr(ctx, "confidence", 1.0),
            "history": list(getattr(ctx, "history", []) or []),
            "user_name": getattr(ctx, "user_name", "Капитан"),
            "pending_intent": getattr(ctx, "pending_intent", None),
        }
        return await process.call(command, payload, dict(slots or {}), timeout)

    handler.__name__ = f"plugin_{plugin_name}_{command.replace('.', '_')}"
    return handler


def _prefixed(name: str, plugin_name: str) -> str:
    """Добавить префикс плагина, если его ещё нет.

    Плагин может назвать навык так же, как сам плагин — тогда префикс
    уже стоит, и повторный дал бы `weather.weather.now`.
    """
    return name if name.startswith(f"{plugin_name}.") else f"{plugin_name}.{name}"


def _build_specs(process: PluginProcess, ready: dict[str, Any],
                 plugin_name: str, timeout: int,
                 taken: set[str]) -> list[IntentSpec]:
    """Настоящие `IntentSpec` ядра из описания, присланного ребёнком.

    Конфликт имён теряет одну команду, а не весь плагин: пять команд
    должны работать, даже если одна совпала с чужой.
    """
    specs: list[IntentSpec] = []
    for skill in ready.get("skills") or []:
        for intent in skill.get("intents") or []:
            name = _prefixed(intent["name"], plugin_name)
            if name in taken:
                log.warning("Плагин %s: команда %s занята, пропущена",
                            plugin_name, name)
                continue
            taken.add(name)
            specs.append(IntentSpec(
                name=name,
                handler=_make_handler(process, intent["name"], plugin_name,
                                      timeout),
                patterns=list(intent.get("patterns") or []),
                examples=list(intent.get("examples") or []),
                slots=_slots_from(intent.get("slots")),
                dangerous=bool(intent.get("dangerous", False)),
                description=intent.get("description", ""),
                priority=int(intent.get("priority", 0)),
                hidden=bool(intent.get("hidden", False)),
            ))
    return specs


async def load_plugin(folder: str | Path, registry: SkillRegistry,
                      taken_names: set[str] | None = None,
                      *, grantor=None) -> LoadedPlugin:
    """Загрузить один плагин.

    Бросает `ManifestError` при любой проблеме — вызывающий решает, что
    с ней делать. Само исключение не должно вылететь в конвейер.
    """
    from . import approvals

    path = Path(folder)
    manifest = load_manifest(path / "manifest.yaml")

    if taken_names is not None and manifest.name in taken_names:
        raise ManifestError(
            f"{path}: плагин «{manifest.name}» уже загружен. "
            "Имя должно быть уникальным — переименуйте папку и name."
        )

    granted = approvals.granted_for(manifest.name, manifest.permissions)
    withheld = tuple(sorted(set(manifest.permissions) - set(granted)))

    # Поле в манифесте называется `timeout_s`. Именно оно, а не `timeout`:
    # опечатка в имени дала бы тихий откат к значению по умолчанию, и
    # плагин с `timeout_s: 5` ждал бы вдвое дольше, чем просил.
    timeout = int(getattr(manifest, "timeout_s", DEFAULT_TIMEOUT_S)
                  or DEFAULT_TIMEOUT_S)
    timeout = max(1, min(timeout, MAX_TIMEOUT_S))

    process = PluginProcess(path, manifest, granted)
    ready = await process.start()

    specs = _build_specs(process, ready, manifest.name, timeout,
                         set(registry.spec_names()))
    if not specs:
        await process.stop()
        raise ManifestError(f"{path}: плагин не зарегистрировал ни одной команды")

    holder = Skill(manifest.name, description=manifest.description)
    holder.intents.extend(specs)
    registry.add(holder)

    if taken_names is not None:
        taken_names.add(manifest.name)

    plugin = LoadedPlugin(
        manifest=manifest,
        process=process,
        commands=tuple(spec.name for spec in specs),
        granted=granted,
        withheld=withheld,
        network_sealed=bool(ready.get("network_sealed", True)),
    )

    log.info("Плагин %s %s: %d команд, прав выдано %s",
             manifest.name, manifest.version, len(specs),
             ", ".join(granted) or "нет")
    if withheld:
        log.info(
            "Плагин %s: не хватает одобрения на %s. Плагин загружен, "
            "но эти возможности будут отказывать.",
            manifest.name, ", ".join(withheld))
    return plugin


async def load_all(plugins_dir: str | Path,
                   registry: SkillRegistry) -> LoadReport:
    """Загрузить все плагины из папки.

    Ни одно исключение отсюда не выходит: неудачный плагин — это запись
    в отчёте, а не падение. Ассистент должен работать с теми плагинами,
    которые удалось, и сказать, какие не загрузились и почему.
    """
    report = LoadReport()
    taken: set[str] = set()

    for folder in discover(plugins_dir):
        try:
            report.loaded.append(
                await load_plugin(folder, registry, taken))
        except ManifestError as exc:
            report.failed.append((folder.name, str(exc)))
            log.error("Плагин %s не загружен: %s", folder.name, exc)
        except Exception as exc:  # noqa: BLE001 — чужой код
            report.failed.append((folder.name, f"{type(exc).__name__}: {exc}"))
            log.error("Плагин %s не загружен, неожиданная ошибка: %s: %s",
                      folder.name, type(exc).__name__, exc)

    log.info("Плагины: %s", report.summary())
    return report


async def reap_idle(report: LoadReport, idle_s: float = IDLE_TIMEOUT_S
                     ) -> list[str]:
    """Закрыть процессы, которые давно молчат.

    Память измерена: ребёнок стоит 27 МБ. Держать их всех ради
    вероятности, что кто-то позовёт, — расточительство; поднять заново
    стоит четверть секунды.
    """
    closed = []
    for plugin in report.loaded:
        process = plugin.process
        if process is None or not process.alive:
            continue
        if process.idle_for >= idle_s:
            await process.stop()
            closed.append(plugin.name)
    if closed:
        log.info("Плагины закрыты по простою: %s", ", ".join(closed))
    return closed


__all__ = [
    "LoadedPlugin",
    "LoadReport",
    "PluginProcess",
    "discover",
    "load_all",
    "load_plugin",
    "reap_idle",
]