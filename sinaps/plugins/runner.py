"""Ребёнок: процесс, в котором исполняется код плагина.

Запускается ядром как `python -m sinaps.plugins.runner`. Всё, что здесь
происходит, происходит **вне** процесса ассистента.

Порядок запуска выбран не по привычке, а по необходимости
------------------------------------------------------
    1. читаем `hello` — какие права одобрены;
    2. грузим `asyncio` и витрину API;
    3. ставим запрет сети и вычищаем кэш импортов;
    4. импортируем модуль плагина и вызываем `register`;
    5. описываем команды и ждём вызовов.

Шаги 2 и 3 идут именно в таком порядке. На Windows `asyncio`
импортирует `socket`, поэтому запрет, поставленный раньше, убивает
цикл событий — а без него не вызвать ни один обработчик. Обратная
очередь тоже не годится: если `socket` уже в кэше импортов, запрет на
него не срабатывает, и плагин проходит мимо. Сначала загрузить,
потом вычистить кэш, потом запретить.

Через границу переносится только то, что уже датакласс
------------------------------------------------------
Ответ — целиком. Из контекста диалога — `text`, `source`,
`confidence`, `history`, `user_name`, `pending_intent`.

А вот `platform` и `session` **не переносятся**. Отдать их плагину
значило бы отдать ему всю машину, и никакой дочерний процесс этого не
компенсирует: объект сокета не сериализуется, а класс платформы даёт
власть над компьютером. Плагин получает ровно то, что перечислил
свой манифест, и ничего сверх того.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

#: Корень проекта. Нужен, чтобы плагин мог сказать
#: `from sinaps.plugins.api import Skill`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

log = logging.getLogger("sinaps.plugins.runner")

#: Поля контекста, которые переносятся через границу.
CTX_FIELDS = ("text", "source", "confidence", "history", "user_name",
              "pending_intent")


def _write(message: dict[str, Any]) -> None:
    from .protocol import write_message

    write_message(sys.stdout.buffer, message)


def _describe_error(exc: BaseException) -> dict[str, Any]:
    """Ошибка в том виде, в котором её можно произнести вслух.

    Тип и текст нужны ядру, чтобы сказать человеку *почему* не
    вышло, а не «что-то пошло не так». Для своих ошибок Sinaps
    сообщение и голос уже готовы, для чужих — берётся текст.
    """
    speech = getattr(exc, "speech", None)
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "speech": speech if isinstance(speech, str) else str(exc),
    }


def _load_entry(folder: Path, module_name: str, function_name: str):
    """Импортировать модуль плагина и достать функцию.

    Папка плагина попадает в `sys.path` на время импорта и убирается
    сразу: иначе два плагина с файлом `plugin.py` подхватили бы друг
    друга.
    """
    folder_str = str(folder)
    sys.path.insert(0, folder_str)
    try:
        sys.modules.pop(module_name, None)
        try:
            module = importlib.import_module(module_name)
        finally:
            sys.modules.pop(module_name, None)
    finally:
        if folder_str in sys.path:
            sys.path.remove(folder_str)

    if not hasattr(module, function_name):
        from .manifest import ManifestError

        raise ManifestError(
            f"{folder}: в модуле «{module_name}» нет функции "
            f"«{function_name}», хотя она указана в entry"
        )
    return getattr(module, function_name)


def _describe_registry(registry) -> list[dict[str, Any]]:
    """Описание команд для ядра: без обработчиков, которые через границу
    не переносятся."""
    described = []
    for skill in registry.skills:
        described.append({
            "name": skill.name,
            "description": skill.description,
            "intents": [{
                "name": spec.name,
                "patterns": list(spec.patterns),
                "examples": list(spec.examples),
                "slots": {
                    slot_name: {
                        "name": slot.name,
                        "type": slot.type,
                        "required": slot.required,
                        "default": slot.default,
                        "description": slot.description,
                        "min_length": slot.min_length,
                    }
                    for slot_name, slot in spec.slots.items()
                },
                "dangerous": spec.dangerous,
                "description": spec.description,
                "priority": spec.priority,
                "hidden": spec.hidden,
            } for spec in skill.intents],
        })
    return described


def _make_context(data: dict[str, Any]):
    """Контекст диалога из пришедших данных.

    `platform` и `session` не восстанавливаются намеренно — см.
    модуль. Их отсутствие — часть контракта, а не недосмотр.
    """
    from ..core.context import DialogContext

    payload = {key: data[key] for key in CTX_FIELDS if key in data}
    payload.setdefault("text", "")
    payload.setdefault("history", [])
    return DialogContext(**payload)


def _make_response(data: dict[str, Any]):
    from ..skills.base import Response

    known = {field: data.get(field) for field in
             ("speech", "display", "follow_up", "ask_for")
             if data.get(field) is not None}
    known["show_panel"] = bool(data.get("show_panel", False))
    meta = data.get("meta")
    known["meta"] = meta if isinstance(meta, dict) else {}
    return Response(**known)


def _reply_error(request_id: int, exc: BaseException) -> None:
    payload = _describe_error(exc)
    payload["id"] = request_id
    payload["op"] = "failed"
    _write(payload)


def main() -> int:
    from .protocol import OP_CALL, OP_FAILED, OP_HELLO, OP_READY, OP_STOP
    from .protocol import OP_RESULT, ProtocolError, read_message
    # Импорт `ManifestError` один и в начале: если импортировать его
    # внутри ветки, имя становится локальным для всей функции, и в другой
    # ветке оно окажется не связанным — `UnboundLocalError` вместо
    # понятной ошибки манифеста.
    from .manifest import ManifestError

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    try:
        hello = read_message(sys.stdin.buffer)
    except ProtocolError as exc:
        _write({"op": OP_FAILED, "phase": "start",
                "type": "ProtocolError", "message": str(exc),
                "speech": "Плагин не запустился"})
        return 2

    if hello.get("op") != OP_HELLO:
        _write({"op": OP_FAILED, "phase": "start", "type": "ProtocolError",
                "message": f"ожидалось приветствие, пришло {hello.get('op')!r}",
                "speech": "Плагин не запустился"})
        return 2

    folder = Path(hello["folder"])
    granted = set(hello.get("permissions") or ())

    # 1. Витрина и цикл событий — до запрета, иначе запрет убьёт asyncio
    from ..core.offline import enforce_offline
    from . import api as _api  # noqa: F401  (нужна импортируемость)

    enforce_offline()

    # 2. Запрет сети, если права не выданы
    from . import sandbox

    network_sealed = False
    if "network.http" not in granted:
        sandbox.arm()
        sealed = sandbox.verify()
        network_sealed = all(sealed.values())
        if not network_sealed:
            log.error("Запрет сети не держит: %s", sealed)

    # 3. Код плагина
    from ..skills.base import Skill
    from ..skills.registry import SkillRegistry

    registry = SkillRegistry()
    handlers: dict[str, Any] = {}

    def _rollback() -> None:
        """Снять всё, что плагин успел добавить.

        Реестр создан пустым, так что снимать нужно всё. Но снимаем
        по самим навыкам, а не по именам команд: навык без единой
        команды в имена бы не попал и остался бы в реестре навсегда.
        """
        for skill in list(registry.skills):
            registry.remove(skill)

    try:
        entry = _load_entry(folder, hello["module"], hello["function"])
        if not callable(entry):
            raise ManifestError(
                f"{folder}: {hello['entry']} — это не функция")

        result = entry(registry)

        if result is not None and not isinstance(result, (Skill, list, tuple)):
            _rollback()
            raise ManifestError(
                f"{folder}: register вернул {type(result).__name__}. "
                "Ожидался Skill, список навыков или ничего."
            )

        for skill in registry.skills:
            for spec in skill.intents:
                handlers[spec.name] = spec.handler

    except Exception as exc:  # noqa: BLE001 — чужой код
        _rollback()
        log.debug("Плагин %s не загрузился:\n%s", folder.name,
                  traceback.format_exc())
        payload = _describe_error(exc)
        payload.update({"op": OP_FAILED, "phase": "register"})
        _write(payload)
        return 1

    _write({
        "op": OP_READY,
        "skills": _describe_registry(registry),
        "network_sealed": network_sealed,
        "granted": sorted(granted),
    })

    # 4. Рабочий цикл
    while True:
        try:
            message = read_message(sys.stdin.buffer)
        except ProtocolError:
            return 0

        op = message.get("op")
        if op == OP_STOP:
            return 0

        if op != OP_CALL:
            _write({"op": OP_FAILED, "phase": "run",
                    "type": "ProtocolError",
                    "message": f"неизвестная операция {op!r}",
                    "speech": "Плагин не понял команду"})
            continue

        request_id = message.get("id")
        handler = handlers.get(message.get("command"))
        if handler is None:
            _reply_error(request_id, KeyError(
                f"команда {message.get('command')!r} не зарегистрирована"))
            continue

        try:
            ctx = _make_context(message.get("ctx") or {})
            slots = message.get("slots") or {}
            response = asyncio.run(handler(ctx, slots))
            _write({"op": OP_RESULT, "id": request_id,
                    "response": _as_dict(response)})
        except Exception as exc:  # noqa: BLE001 — чужой код
            log.debug("Плагин %s упал на %s:\n%s", folder.name,
                      message.get("command"), traceback.format_exc())
            _reply_error(request_id, exc)

    return 0


def _as_dict(response) -> dict[str, Any]:
    """Ответ плагина в том виде, в каком поедет через границу.

    `speech` обязателен: без него ассистент не сможет ничего произнести,
    и тишина была бы хуже любого текста.
    """
    if response is None:
        raise TypeError("обработчик вернул None, ожидался Response")

    data = {
        "speech": getattr(response, "speech", None) or "",
        "display": getattr(response, "display", None),
        "follow_up": getattr(response, "follow_up", None),
        "ask_for": getattr(response, "ask_for", None),
        "show_panel": bool(getattr(response, "show_panel", False)),
    }
    meta = getattr(response, "meta", None)
    if isinstance(meta, dict):
        try:
            json.dumps(meta)
        except (TypeError, ValueError):
            meta = {}
        data["meta"] = meta
    return data


if __name__ == "__main__":
    sys.exit(main())