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

from ..core.bus import EventBus, EventType
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
#: Замерено на живой машине, а не оценено: ребёнок стоит 4 МБ рабочей
#: памяти — и столько же на настоящем плагине погоды, не только на пустом
#: тестовом. Десять плагинов — это 40 МБ, которые никто не просил.
#: Подъём заново — 0.14 с.
#:
#: Раньше здесь стояло 27 МБ и «четверть секунды». Оба числа были взяты
#: на глаз и завышали цену уборки в семь раз: она выглядела дороже, чем
#: на самом деле стоит.
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

    async def stop(self, reason: str = "stop") -> None:
        if self.process is not None:
            await self.process.stop(reason)


@dataclass(frozen=True)
class PluginStats:
    """Плагины числом и весом.

    Отдельная сущность, а не пара чисел в логе: трей и будущее окно
    «Плагины» будут читать те же поля, и пересчитывать всё заново
    каждый раз — значит разойтись с тем, что показал лог.
    """

    #: Загружено всего.
    total: int
    #: Процессы сейчас живы и ждут вызова.
    alive: int
    #: Процессы закрыты по простою.
    asleep: int
    #: Суммарная рабочая память живых процессов.
    rss_mb: float

    def line(self) -> str:
        """Строка для журнала."""
        return (f"плагинов {self.total} (живых {self.alive}, "
                f"спящих {self.asleep}), память {self.rss_mb:.1f} МБ")


def plugin_stats(report: LoadReport) -> PluginStats:
    """Посчитать живые процессы плагинов и их суммарную память.

    Память меряется у живых процессов: у закрытого `psutil` отвечает
    `NoSuchProcess`, и это не ошибка, а признак того, что процесс
    действительно закрыт. Помеченный «спящим» плагин в сумму не входит —
    иначе счётчик памяти врал бы ровно тогда, когда память освободили.

    Раньше эти числа брались на глаз и были завышены в семь раз. Теперь
    их можно увидеть, а не поверить.
    """
    import psutil

    alive = 0
    asleep = 0
    rss = 0
    for plugin in report.loaded:
        process = plugin.process
        if process is None or not process.alive:
            asleep += 1
            continue
        alive += 1
        pid = process.pid
        if pid is None:
            continue
        try:
            rss += psutil.Process(pid).memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            # Процесс только что закрылся. Падать из-за этого нельзя:
            # метрика не должна ронять ассистента.
            continue

    return PluginStats(
        total=len(report.loaded),
        alive=alive,
        asleep=asleep,
        rss_mb=rss / (1024 * 1024),
    )


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

    async def stop_all(self, reason: str = "stop") -> None:
        """Закрыть все дочерние процессы. Вызывается при остановке."""
        for plugin in self.loaded:
            try:
                await plugin.stop(reason)
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
                 granted: Sequence[str] = (),
                 bus: "EventBus | None" = None) -> None:
        self.folder = folder
        self.manifest = manifest
        self.granted = tuple(granted)
        #: Шина для событий жизненного цикла. Необязательна: загрузчик
        #: используется и в тестах, где события никому не нужны, а
        #: подставлять заглушку в каждый вызов — лишнее.
        self.bus = bus
        self._proc: Any = None
        #: Поднимался ли плагин хоть раз. От этого зависит, слышать ли
        #: подписчикам о неудачном пробуждении: первая загрузка и повторная
        #: после сна — разные события для человека.
        self._woke_before = False
        self._next_id = 0
        self._last_used = 0.0
        #: Идёт ли сейчас обмен с ядром. Простоя недостаточно, чтобы
        #: убирать процесс во время вызова: `_last_used` обновляется в
        #: конце вызова, и весь разговор с плагином «стареет».
        self.busy = False
        self.ready: dict[str, Any] = {}
        # Состояние привязано к имени плагина, а не к папке: переименовать
        # папку не значит начать с нуля, и два плагина с разными именами
        # не видят данных друг друга даже если лежат рядом.
        from .state import PluginState

        self._state = PluginState(plugin=manifest.name)

    # --- жизненный цикл ----------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        """PID дочернего процесса или `None`, если он не запущен."""
        return self._proc.pid if self.alive and self._proc is not None else None

    async def _emit(self, event: EventType, **payload: Any) -> None:
        """Отправить событие жизненного цикла, если шина есть.

        Подписчикам (трей, а в Beta 1.1 — окно «Плагины») нужно знать,
        что плагин поднялся или уснул: иначе состояние «спит» пришлось бы
        угадывать опросом процессов.

        Ошибка отправки не должна мешать: событие — это рассказ о том,
        что произошло, а не часть самого происходящего.
        """
        if self.bus is None:
            return
        try:
            await self.bus.emit(event, plugin=self.manifest.name, **payload)
        except Exception:  # noqa: BLE001 — подписчик не должен ронять плагин
            log.debug("Событие %s не доставлено", event.value, exc_info=True)

    @property
    def idle_for(self) -> float:
        if self._proc is None:
            return 0.0
        return asyncio.get_running_loop().time() - self._last_used

    async def start(self) -> dict[str, Any]:
        """Поднять процесс и получить описание команд."""
        if self.alive and self.ready:
            return self.ready

        self._woke_before = True

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
        except (asyncio.TimeoutError, ProtocolError,
                asyncio.IncompleteReadError) as exc:
            # Плагин, который уже работал и не смог снова подняться, —
            # это пробуждение, а не первая загрузка. Разница видна
            # человеку: в одном случае сбой при старте ассистента, в
            # другом посреди работы, и ждать придётся не перезапуска.
            if self._woke_before:
                await self._emit(EventType.PLUGIN_WAKE_FAILED,
                                 error=f"{type(exc).__name__}: {exc}")
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
            await self._emit(
                EventType.PLUGIN_LOADED,
                skills=len(self.ready.get("skills", [])))
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
        from .protocol import OP_CAP, OP_RESULT, ProtocolError
        from .protocol import read_message_async, write_message_async

        await self.start()
        assert self._proc is not None

        self._next_id += 1
        request_id = self._next_id

        # На всё время обмена процесс считается занятым, и уборщик его не
        # трогает. Одной проверки по времени не хватило бы: плагину
        # разрешено работать `MAX_TIMEOUT_S` (300 с), а простой закрывают
        # после 120 — легитимный долгий вызов выглядел бы как простой и
        # был убит посреди работы. `_last_used` обновляется в конце
        # вызова, поэтому весь разговор с плагином «стареет».
        self.busy = True
        try:
            await write_message_async(self._proc.stdin, {
                "op": "call",
                "id": request_id,
                "command": command,
                "ctx": ctx,
                "slots": slots or {},
            })

            # Пока идёт обработчик, ребёнок может позвать возможность ядра:
            # прочитать ключ состояния, записать, спросить список. Поэтому
            # читаем не один кадр, а цикл — и каждый промежуточный кадр
            # выполняем и отвечаем. Первая версия читала один кадр и
            # принимала его за ответ: первый же `ctx.state.get` ломал бы
            # разговор ошибкой «пришёл ответ не на тот вызов».
            deadline = asyncio.get_running_loop().time() + timeout
            try:
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    message = await asyncio.wait_for(
                        read_message_async(self._proc.stdout), remaining)

                    if message.get("op") == OP_CAP:
                        await self._serve_capability(message)
                        continue
                    break

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
                    f"Плагин «{self.manifest.name}»: пришёл ответ "
                    "не на тот вызов",
                    speech="Что-то пошло не так с плагином")

            if message.get("op") == OP_RESULT:
                return _response_from(message.get("response") or {})

            from ..core.errors import PluginError

            raise PluginError(
                f"Плагин «{self.manifest.name}»: "
                f"{message.get('message', 'неизвестная ошибка')}",
                speech=message.get("speech")
                or f"Плагин «{self.manifest.name}» не сработал")
        finally:
            # Без `finally` флаг залип бы навсегда после обрыва записи:
            # процесс считался бы занятым и его больше никто не убрал бы.
            self.busy = False

    async def _serve_capability(self, message: dict[str, Any]) -> None:
        """Выполнить просьбу плагина и ответить.

        Право проверяется **до** похода в базу, а не после. Проверка
        после дала бы плагину лишний доступ: он уже выполнил бы запрос,
        и запрет означал бы «результат не покажем», а не «не делай».
        """
        assert self._proc is not None
        from ..core.errors import PermissionDenied
        from .protocol import CAP_STATE, OP_CAP_FAIL, OP_CAP_OK
        from .protocol import write_message_async

        try:
            cap = message.get("cap")
            if cap != CAP_STATE:
                raise PermissionDenied(str(cap or "неизвестное"))
            if CAP_STATE not in self.granted:
                raise PermissionDenied(CAP_STATE, self.manifest.name)
            reply = {"op": OP_CAP_OK,
                     "value": _state_action(self._state,
                                            message.get("action") or "",
                                            message.get("kwargs") or {})}
        except PermissionDenied as exc:
            reply = {"op": OP_CAP_FAIL, "type": "PermissionDenied",
                     "permission": exc.permission, "message": str(exc),
                     "speech": exc.speech}
        except Exception as exc:  # noqa: BLE001 — наш код и наша база
            log.warning("Плагин %s: возможность не сработала — %s: %s",
                        self.manifest.name, type(exc).__name__, exc)
            reply = {"op": OP_CAP_FAIL, "type": type(exc).__name__,
                     "message": str(exc),
                     "speech": "Плагин не смог воспользоваться состоянием"}

        try:
            await write_message_async(self._proc.stdin, reply)
        except (ConnectionError, BrokenPipeError, AttributeError) as exc:
            log.warning("Плагин %s: ответ на возможность не доставлен — %s",
                        self.manifest.name, exc)

    async def stop(self, reason: str = "stop") -> None:
        """Закрыть процесс. Молчаливый провал здесь — норма.

        `reason` попадает в событие `PLUGIN_UNLOADED`: «idle» — уснул по
        простоям, «stop» — погасили нарочно. Разница важна подписчику:
        по ней видно, что плагин освобождал память сам или его закрыли
        при остановке ассистента.

        Событие отправляется один раз на реально живший процесс: повторный
        `stop()` и остановка уже мёртвого процесса событий не порождают,
        иначе счётчик засыпаний в окне разъезжался бы с реальностью.
        """
        from .protocol import OP_STOP, ProtocolError, write_message_async

        proc, self._proc = self._proc, None
        self.ready = {}
        if proc is None or proc.returncode is not None:
            return

        await self._emit(EventType.PLUGIN_UNLOADED, reason=reason)

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


#: Что плагин может просить у своего состояния.
#:
#: Список явный, а не «любой метод объекта»: иначе появление нового
#: метода в `PluginState` молча открывало бы плагину новое право, и
#: никто бы об этом не узнал.
_STATE_ACTIONS = ("get", "set", "delete", "keys", "all", "clear")


def _state_action(state, action: str, kwargs: dict[str, Any]) -> Any:
    """Одна операция состояния."""
    if action not in _STATE_ACTIONS:
        from ..core.errors import PermissionDenied

        raise PermissionDenied(f"state.{action}")

    handler = getattr(state, action)
    if action in ("keys", "all", "clear"):
        return handler()
    return handler(**kwargs)


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
                      *, grantor=None,
                      bus: "EventBus | None" = None) -> LoadedPlugin:
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

    process = PluginProcess(path, manifest, granted, bus=bus)
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


async def load_all(plugins_dir: str | Path, registry: SkillRegistry,
                   bus: "EventBus | None" = None) -> LoadReport:
    """Загрузить все плагины из папки.

    Ни одно исключение отсюда не выходит: неудачный плагин — это запись
    в отчёте, а не падение. Ассистент должен работать с теми плагинами,
    которые удалось, и сказать, какие не загрузились и почему.

    `bus` необязательна и передаётся плагинам, чтобы те сами сообщали о
    своём подъёме. Без неё всё работает, просто никто не узнает.
    """
    report = LoadReport()
    taken: set[str] = set()

    for folder in discover(plugins_dir):
        try:
            report.loaded.append(
                await load_plugin(folder, registry, taken, bus=bus))
        except ManifestError as exc:
            report.failed.append((folder.name, str(exc)))
            log.error("Плагин %s не загружен: %s", folder.name, exc)
        except Exception as exc:  # noqa: BLE001 — чужой код
            report.failed.append((folder.name, f"{type(exc).__name__}: {exc}"))
            log.error("Плагин %s не загружен, неожиданная ошибка: %s: %s",
                      folder.name, type(exc).__name__, exc)

    log.info("Плагины: %s", report.summary())
    log.info("Плагины в памяти: %s", plugin_stats(report).line())
    return report


async def reap_idle(report: LoadReport,
                     idle_s: float | None = IDLE_TIMEOUT_S) -> list[str]:
    """Закрыть процессы, которые давно молчат.

    Память измерена: ребёнок стоит 4 МБ, подъём заново — 0.14 с.
    Держать процессы ради вероятности, что кто-то позовёт, —
    расточительство.

    `idle_s=None` означает «не убирать никогда» — выключатель для того,
    кому +0.25 с на следующий запрос мешает больше, чем память.

    Ноль порогом не является и убирает всё: `idle_for >= 0` верно всегда.
    Тесты этим пользуются намеренно.

    Занятые процессы пропускаются: уборка посреди вызова оборвала бы
    разговор с плагином, которому ещё не истек собственный таймаут.
    """
    if idle_s is None:
        return []

    closed = []
    for plugin in report.loaded:
        process = plugin.process
        if process is None or not process.alive or process.busy:
            continue
        if process.idle_for >= idle_s:
            # `reason="idle"`: подписчик отличит плагин, который сам
            # освободил память, от погашенного при остановке ассистента.
            await process.stop(reason="idle")
            closed.append(plugin.name)
    if closed:
        log.info("Плагины закрыты по простою: %s", ", ".join(closed))
        log.info("Плагины в памяти: %s", plugin_stats(report).line())
    return closed


__all__ = [
    "LoadedPlugin",
    "LoadReport",
    "PluginProcess",
    "PluginStats",
    "discover",
    "load_all",
    "load_plugin",
    "plugin_stats",
    "reap_idle",
]