"""Своё состояние плагина: ключи и значения в SQLite.

Почему не `facts` из истории
----------------------------
В `memory/store.py` есть таблица `facts` — факты о пользователе, ключ
уникален. Выглядит соблазнительно положить состояние плагина туда же с
префиксом `weather:`.

Так нельзя. Таблица одна на всех, ключи видны всем, и плагин, угадавший
чужой ключ, прочитал бы то, что ему не показывали. Пространство имён
на префиксе держится на том, что каждый вспомнет свой префикс, а
приватность держится на правах — а права здесь как раз не выданы.

Поэтому своя таблица с `(plugin, key)` и своя база `data/plugins.db`.
Два файла вместо одного — цена за то, что состояние плагинов и память
о собеседнике не делят ни таблицу, ни путь.

Что можно хранить
-----------------
Только JSON: строки, числа, списки, словари, `true/false/null`.
Двоичные данные, объекты и файлы — нельзя. Плагин работает в другом
процессе, значение пересекает границу как JSON, и попытка засунуть
туда что-то ещё должна падать здесь, с понятным объяснением, а не
в молчаливую потерю на сериализации.

Процессов может быть много
--------------------------
Каждый плагин — свой дочерний процесс, и все они пишут в одну базу.
Поэтому запись короткая и под блокировкой: потерянная запись лучше
зависшего голосового диалога.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("sinaps.plugins.state")

FILENAME = "plugins.db"
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_state (
    plugin   TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    updated  REAL NOT NULL,
    PRIMARY KEY (plugin, key)
);
CREATE INDEX IF NOT EXISTS idx_plugin_state_plugin ON plugin_state(plugin);
"""

#: Ключ должен быть непустой и не слишком длинным.
#:
#: Проверка до записи, а не при чтении: плагин может загадать ключ из
#: пользовательского ввода, и сначала надо убедиться, что это вообще
#: строка, а уже потом идти в базу.
MAX_KEY_LENGTH = 200


class StateError(Exception):
    """Значение не годится для хранения."""


def default_path() -> Path:
    from ..core.config import DATA_DIR

    return Path(DATA_DIR) / FILENAME


def _check_key(key: Any) -> str:
    if not isinstance(key, str) or not key.strip():
        raise StateError("ключ должен быть непустой строкой")
    if len(key) > MAX_KEY_LENGTH:
        raise StateError(
            f"ключ длиннее {MAX_KEY_LENGTH} символов: {len(key)}")
    return key


def _check_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise StateError(
            f"значение нельзя сохранить: {exc}. Допустимы только "
            "строки, числа, списки, словари, true/false/null"
        ) from exc


class PluginState:
    """Состояние одного плагина.

    На каждый вызов — своё соединение, как в `MemoryStore`. Медленнее
    общей копии, зато не требует ни блокировок на стороне event loop,
    ни care'а о том, кто закрывает базу. Для объёма «десятки ключей»
    разница не измерима.
    """

    def __init__(self, path: str | Path | None = None,
                 plugin: str = "") -> None:
        self.path = Path(path) if path is not None else default_path()
        self.plugin = plugin
        self._lock = threading.Lock()
        self._ready = False

    # --- подключение ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            self._ready = True

    # --- операции ----------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Значение или `default`, если ключа нет.

        Непустое, но неразбираемое значение — тоже отсутствие: битая
        запись не должна ронять голосовой диалог.
        """
        key = _check_key(key)
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM plugin_state WHERE plugin=? AND key=?",
                    (self.plugin, key)).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.warning("Состояние плагина %s: чтение не удалось (%s)",
                        self.plugin, exc)
            return default

        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            log.warning("Состояние плагина %s: ключ %s не читается",
                        self.plugin, key)
            return default

    def set(self, key: str, value: Any) -> None:
        """Записать значение. Ошибка БД глушится, но логируется."""
        key = _check_key(key)
        payload = _check_value(value)

        import time

        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO plugin_state(plugin, key, value, updated) "
                    "VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(plugin, key) DO UPDATE SET "
                    "value=excluded.value, updated=excluded.updated",
                    (self.plugin, key, payload, time.time()))
                conn.commit()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.warning("Состояние плагина %s: запись не удалась (%s)",
                        self.plugin, exc)

    def delete(self, key: str) -> bool:
        """Убрать ключ. `False` — его и не было."""
        key = _check_key(key)
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM plugin_state WHERE plugin=? AND key=?",
                    (self.plugin, key))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.warning("Состояние плагина %s: удаление не удалось (%s)",
                        self.plugin, exc)
            return False

    def keys(self) -> list[str]:
        """Все ключи этого плагина."""
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT key FROM plugin_state WHERE plugin=? "
                    "ORDER BY key", (self.plugin,)).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.warning("Состояние плагина %s: список не получен (%s)",
                        self.plugin, exc)
            return []
        return [row[0] for row in rows]

    def all(self) -> dict[str, Any]:
        """Всё состояние плагина целиком."""
        return {key: self.get(key) for key in self.keys()}

    def clear(self) -> int:
        """Стереть всё состояние плагина. Сколько ключей убрано."""
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM plugin_state WHERE plugin=?",
                    (self.plugin,))
                conn.commit()
                return int(cursor.rowcount or 0)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.warning("Состояние плагина %s: очистка не удалась (%s)",
                        self.plugin, exc)
            return 0