"""Кто и на что согласен.

Файл `data/plugins.json` — единственное место, где живут одобрения.
Он не в репозитории и не в коде: человек выдаёт права осознанно, и
решение принадлежит ему, а не сборке.

Зачем хеш набора прав
---------------------
Наивная версия хранит только список прав. Она дырявая, и дыра
простая: автор сначала пишет в манифест `state`, его одобряют, потом
дописывает `network.http` — а одобрение то же, и права расширились
сами. Человек, который смотрел на список в момент одобрения, новых
прав не видел.

Поэтому рядом с правами лежит `permissions_hash` — отпечаток того
списка. Набор изменился — хеш другой — одобрение не действует, и
плагин грузится без новых прав. Правописание описания, номер версии,
перестановка строк в манифесте на одобрение не влияют: хеш считается
по отсортированному списку прав, а не по файлу целиком.

Почему отказ не отменяет прежние права
--------------------------------------
Если человек убрал разрешение, а плагин без него не запускается, то
отзыв права ломает плагин целиком. Здесь мягче: неодобренные права
просто **не выдаются**, плагин грузится и работает, а обращение к
запрещённому бросает `NetworkDisabled` с понятным объяснением. Так
отзыв одного права не превращается в отказ всего плагина.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("sinaps.plugins.approvals")

#: Версия файла. Записана, чтобы будущая смена формата не читалась
#: молча: незнакомую версию надо назвать, а не угадать.
FILE_VERSION = 1

FILENAME = "plugins.json"


def _data_dir() -> Path:
    from ..core.config import DATA_DIR

    return Path(DATA_DIR)


def path() -> Path:
    return _data_dir() / FILENAME


def permissions_hash(permissions) -> str:
    """Отпечаток набора прав.

    Считается по отсортированному списку, а не по тексту манифеста:
    перестановка строк и правка описания не должны требовать нового
    одобрения, а добавление права — должно.
    """
    payload = "\n".join(sorted(permissions)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def load() -> dict[str, Any]:
    """Прочитать файл одобрений. Отсутствие файла — не поломка."""
    target = path()
    if not target.exists():
        return {"version": FILE_VERSION, "plugins": {}}

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Файл одобрений не читается (%s): %s", target, exc)
        return {"version": FILE_VERSION, "plugins": {}}

    if not isinstance(data, dict) or not isinstance(data.get("plugins"), dict):
        log.error("Файл одобрений имеет неожиданный вид: %s", target)
        return {"version": FILE_VERSION, "plugins": {}}

    return data


def save(data: dict[str, Any]) -> None:
    """Записать файл одобрений."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def granted_for(name: str, permissions) -> tuple[str, ...]:
    """Какие права у плагина `name` действительно действуют.

    Возвращает пусто, если плагина в файле нет, если хеш не совпал или
    если файл не читается. Молча выдать права при несовпадении хеша
    нельзя — тогда одобрение переживает изменение манифеста, и весь
    смысл проверки в том, чтобы этого не случилось.
    """
    entry = load()["plugins"].get(name)
    if not isinstance(entry, dict):
        return ()

    stored_hash = entry.get("permissions_hash")
    current = permissions_hash(permissions)
    if stored_hash != current:
        if stored_hash is not None:
            log.info(
                "Плагин %s изменил набор прав, нужно новое одобрение: "
                "было %s, стало %s",
                name, entry.get("permissions") or [], list(permissions))
        return ()

    granted = entry.get("permissions")
    if not isinstance(granted, list):
        return ()

    # Выдаём только то, что плагин действительно объявил: в файле
    # могли появиться права, которых в манифесте уже нет.
    declared = set(permissions)
    return tuple(p for p in granted if p in declared)


def grant(name: str, permissions) -> None:
    """Записать одобрение набора прав."""
    data = load()
    data["version"] = FILE_VERSION
    data["plugins"][name] = {
        "permissions": sorted(permissions),
        "permissions_hash": permissions_hash(permissions),
        "granted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save(data)
    log.info("Плагин %s одобрен на права: %s", name, ", ".join(
        sorted(permissions)) or "нет")


def revoke(name: str) -> bool:
    """Отозвать одобрение. `False` — одобрения и не было."""
    data = load()
    if name not in data["plugins"]:
        return False
    del data["plugins"][name]
    save(data)
    log.info("Одобрение плагина %s отозвано", name)
    return True


def approved() -> dict[str, list[str]]:
    """Всё, что одобрено — для панели возможностей."""
    return {
        name: list(entry.get("permissions") or [])
        for name, entry in load()["plugins"].items()
        if isinstance(entry, dict)
    }


def missing_approval(name: str, permissions) -> tuple[str, ...]:
    """Что именно надо подтвердить — для панели и сообщения."""
    have = set(granted_for(name, permissions))
    return tuple(sorted(p for p in permissions if p not in have))