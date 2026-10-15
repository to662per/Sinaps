"""История диалогов в SQLite.

Зачем отдельный слой
--------------------
История нужна не «для галочки». Три применения:

  * окно истории — посмотреть, что было вчера;
  * `v1` — факты о пользователе и таймеры должны пережить перезапуск;
  * отладка — когда пользователь говорит «не работает», видно, что именно
    он наговорил, а не только что система ответила.

Принципы
--------
* **Никаких сетевых вызовов.** Файл локальный, `privacy.local_only`.
* **Запись не должна блокировать голос.** Пишем из event loop, поэтому
  соединение — в отдельном потоке с короткими транзакциями.
* **История не должна расти вечно.** `keep_history_days` чистит старое
  при старте и раз в сутки.
* **Отказ не должен ломать разговор.** Любая ошибка БД глушится: потеря
  одной записи лучше, чем молчащий ассистент.

Схема
-----
    dialogues   одна реплика: кто сказал, что, когда и с какой задержкой
    turns       один законченный обмен (реплика + ответ) для сводки
    facts       факты о пользователе (v1)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Версия схемы. При изменении структуры — увеличить и написать миграцию.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    role        TEXT    NOT NULL,      -- user | assistant
    text        TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT '',   -- voice | text
    confidence  REAL    NOT NULL DEFAULT 0.0,
    intent      TEXT    NOT NULL DEFAULT '',
    latency_ms  INTEGER NOT NULL DEFAULT 0,
    meta        TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_dialogues_ts ON dialogues(ts);

CREATE TABLE IF NOT EXISTS turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    REAL NOT NULL,
    user_text     TEXT NOT NULL,
    bot_text      TEXT NOT NULL DEFAULT '',
    intent        TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    total_ms      INTEGER NOT NULL DEFAULT 0,
    user_dialogue_id INTEGER,
    bot_dialogue_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_turns_started ON turns(started_at);

CREATE TABLE IF NOT EXISTS facts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    key       TEXT NOT NULL UNIQUE,
    value     TEXT NOT NULL,
    updated   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Entry:
    """Одна запись истории."""

    id: int
    ts: float
    role: str            # user | assistant
    text: str
    source: str = ""
    confidence: float = 0.0
    intent: str = ""
    latency_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%H:%M:%S")

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%d.%m.%Y")


@dataclass(frozen=True)
class Turn:
    """Один законченный обмен: реплика и ответ."""

    id: int
    started_at: float
    user_text: str
    bot_text: str = ""
    intent: str = ""
    source: str = ""
    total_ms: int = 0

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.started_at).strftime("%H:%M")


class MemoryStore:
    """Потокобезопасная обёртка над SQLite.

    Каждый вызов открывает своё соединение. Это медленнее общей копии,
    но зато не требует ни блокировок на стороне event loop, ни care'а о
    том, кто и когда закрывает базу. Для объёма «несколько десятков
    реплик в день» разница не измерима.
    """

    def __init__(self, path: str | Path, *, keep_days: int = 30) -> None:
        self.path = Path(path)
        self.keep_days = max(0, int(keep_days))
        self._lock = threading.Lock()
        self._ready = False

    # --- подключение ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # WAL: чтение из окна истории не блокирует запись новых реплик.
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass  # не критично: работаем и без WAL
        return conn

    def _ensure_schema(self) -> None:
        """Создаёт схему. Идемпотентна и защищена вызывающим.

        Бросает OSError, если каталог создать нельзя: вызывающий
        перехватывает и превращает это в пустой результат.
        """
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                conn.commit()
                self._ready = True
                log.info("Хранилище истории готово: %s", self.path)
            finally:
                conn.close()

    def _write(self, sql: str, params: tuple) -> int | None:
        """Запись без опасений. Ошибка БД не должна ронять разговор."""
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                cursor = conn.execute(sql, params)
                conn.commit()
                return int(cursor.lastrowid or 0) or None
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            # OSError важен не меньше sqlite3.Error: `mkdir` может упереться
            # в файл на месте каталога и бросить FileExistsError, который
            # иначе улетел бы из хранилища прямо в голосовой контур.
            log.warning("История: запись не удалась (%s)", exc)
            return None

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.warning("История: чтение не удалось (%s)", exc)
            return []

    # --- реплики ----------------------------------------------------------

    def add_dialogue(
        self,
        role: str,
        text: str,
        *,
        source: str = "",
        confidence: float = 0.0,
        intent: str = "",
        latency_ms: int = 0,
        meta: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> int | None:
        """Сохранить одну реплику. `role`: 'user' | 'assistant'."""
        # BOM вычищаем и здесь: он незаметен в поиске по истории, но
        # переносится в любой экспорт, и запись становится мусором.
        text = (text or "").strip().lstrip("﻿")
        if not text:
            return None
        return self._write(
            "INSERT INTO dialogues(ts, role, text, source, confidence, intent,"
            " latency_ms, meta) VALUES(?,?,?,?,?,?,?,?)",
            (ts if ts is not None else time.time(), role, text, source,
             float(confidence), intent, int(latency_ms),
             json.dumps(meta or {}, ensure_ascii=False)),
        )

    def recent(self, *, limit: int = 100, role: str | None = None) -> list[Entry]:
        """Последние реплики, свежие сверху."""
        sql = "SELECT * FROM dialogues"
        params: list = []
        if role:
            sql += " WHERE role = ?"
            params.append(role)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [self._row_to_entry(r) for r in self._read(sql, tuple(params))]

    def search(self, query: str, *, limit: int = 100) -> list[Entry]:
        """Поиск подстроки по всем репликам."""
        needle = f"%{(query or '').strip()}%"
        return [
            self._row_to_entry(r)
            for r in self._read(
                "SELECT * FROM dialogues WHERE text LIKE ?"
                " ORDER BY ts DESC, id DESC LIMIT ?",
                (needle, max(1, int(limit))),
            )
        ]

    def history(self, *, query: str = "", role: str = "", intent: str = "",
                since: float | None = None, until: float | None = None,
                limit: int = 300) -> list[Entry]:
        """История с фильтрами — для вкладки в окне.

        `LIKE`, а не FTS5: полнотекстовый индекс на русском требует
        отдельного словаря и ломался бы о слова вроде «ёжик» и «взять».
        Критерий версии — работающий поиск на 1000 диалогах, а не
        индекс; измерять скорость на живом объёме раньше, чем понятно,
        что LIKE не хватает, — значит платить за лишнюю схему.

        Порядок составных условий повторяет разговор: сначала что
        сказано, потом кто, потом когда. Неизбежно `LIKE` по каждому
        условию, но список невелик и отдаётся с ограничением.
        """
        sql = "SELECT * FROM dialogues"
        clauses: list[str] = []
        params: list = []

        text = (query or "").strip()
        if text:
            clauses.append("text LIKE ?")
            params.append(f"%{text}%")

        who = (role or "").strip()
        if who:
            clauses.append("role = ?")
            params.append(who)

        name = (intent or "").strip()
        if name:
            clauses.append("intent = ?")
            params.append(name)

        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("ts <= ?")
            params.append(float(until))

        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))

        return [self._row_to_entry(r) for r in self._read(sql, tuple(params))]

    def history_stats(self, *, days: int = 7) -> dict[str, Any]:
        """Сводка для вкладки «История».

        Считается по обменам, а не по отдельным репликам: «сколько фраз
        за неделю» про обмены, и считать реплики значит удвоить число и
        показать человеку то, чего он не говорил.
        """
        since = time.time() - max(1, int(days)) * 86400

        turns = self._read(
            "SELECT intent, AVG(total_ms) AS avg_ms, COUNT(*) AS n"
            " FROM turns WHERE started_at >= ? GROUP BY intent",
            (since,),
        )

        by_intent = sorted(
            ({"intent": r["intent"] or "—",
              "count": int(r["n"]),
              "avg_ms": int(r["avg_ms"] or 0)} for r in turns),
            key=lambda item: (-item["count"], item["intent"]),
        )

        total_row = self._read(
            "SELECT COUNT(*) AS n, AVG(total_ms) AS avg_ms FROM turns"
            " WHERE started_at >= ?", (since,))
        total_turns = int(total_row[0]["n"]) if total_row else 0
        avg_total = int(total_row[0]["avg_ms"] or 0) if total_row else 0

        day_row = self._read(
            "SELECT COUNT(*) AS n FROM dialogues WHERE ts >= ?", (since,))
        phrases = int(day_row[0]["n"]) if day_row else 0

        return {
            "days": int(days),
            "phrases": phrases,
            "turns": total_turns,
            "avg_ms": avg_total,
            "top_intents": by_intent[:5],
        }

    def export(self, path: str | Path, *, query: str = "", role: str = "",
               intent: str = "") -> int:
        """Выгрузить историю в JSON или TXT. Возвращает число записей.

        Формат выбирается по расширению: `.json` — разбираемо
        машиной, всё прочее — читаемо человеком. Явный выбор формата
        не сделан намеренно: человек зовёт «выгрузить» и ждёт файл,
        который сможет открыть.
        """
        entries = self.history(query=query, role=role, intent=intent,
                               limit=100000)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.suffix.lower() == ".json":
            payload = [
                {"ts": e.ts, "when": datetime.fromtimestamp(e.ts).isoformat(),
                 "role": e.role, "text": e.text, "source": e.source,
                 "intent": e.intent, "confidence": e.confidence,
                 "latency_ms": e.latency_ms}
                for e in entries
            ]
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8")
        else:
            lines = []
            for entry in entries:
                when = datetime.fromtimestamp(entry.ts).strftime(
                    "%d.%m.%Y %H:%M:%S")
                who = "Вы" if entry.role == "user" else "Син"
                lines.append(f"[{when}] {who}: {entry.text}")
            target.write_text("\n".join(lines), encoding="utf-8")

        return len(entries)

    def by_day(self, days: int = 7) -> dict[str, int]:
        """Сколько реплик было в каждый из последних дней."""
        since = time.time() - max(1, int(days)) * 86400
        rows = self._read(
            "SELECT ts FROM dialogues WHERE ts >= ? ORDER BY ts", (since,))
        buckets: dict[str, int] = {}
        for row in rows:
            day = datetime.fromtimestamp(row["ts"]).strftime("%d.%m.%Y")
            buckets[day] = buckets.get(day, 0) + 1
        return buckets

    # --- обмены -----------------------------------------------------------

    def add_turn(
        self,
        user_text: str,
        bot_text: str = "",
        *,
        intent: str = "",
        source: str = "",
        total_ms: int = 0,
        started_at: float | None = None,
    ) -> int | None:
        """Сохранить законченный обмен (для сводки и статистики)."""
        user_text = (user_text or "").strip().lstrip("﻿")
        if not user_text:
            return None
        return self._write(
            "INSERT INTO turns(started_at, user_text, bot_text, intent, source,"
            " total_ms) VALUES(?,?,?,?,?,?)",
            (started_at if started_at is not None else time.time(),
             user_text, (bot_text or "").strip(), intent, source,
             int(total_ms)),
        )

    def recent_turns(self, *, limit: int = 50) -> list[Turn]:
        rows = self._read(
            "SELECT * FROM turns ORDER BY started_at DESC, id DESC LIMIT ?",
            (max(1, int(limit)),))
        return [
            Turn(id=r["id"], started_at=r["started_at"], user_text=r["user_text"],
                 bot_text=r["bot_text"] or "", intent=r["intent"] or "",
                 source=r["source"] or "", total_ms=r["total_ms"] or 0)
            for r in rows
        ]

    # --- факты (v1) -------------------------------------------------------

    def set_fact(self, key: str, value: str) -> None:
        """Запомнить факт о пользователе: имя, любимый город и т.п."""
        self._write(
            "INSERT INTO facts(key, value, updated) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated=excluded.updated",
            (key, value, time.time()),
        )

    def get_fact(self, key: str, default: str = "") -> str:
        rows = self._read("SELECT value FROM facts WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def all_facts(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._read("SELECT key, value FROM facts")}

    # --- обслуживание ------------------------------------------------------

    def prune(self) -> int:
        """Удалить старые реплики. Возвращает количество удалённых."""
        if self.keep_days <= 0:
            return 0
        cutoff = time.time() - self.keep_days * 86400
        before = self._count("dialogues")
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                conn.execute("DELETE FROM dialogues WHERE ts < ?", (cutoff,))
                conn.execute("DELETE FROM turns WHERE started_at < ?", (cutoff,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("История: очистка не удалась (%s)", exc)
            return 0
        removed = before - self._count("dialogues")
        if removed:
            log.info("История: удалено старых реплик — %d", removed)
        return removed

    def clear(self) -> None:
        """Стереть всю историю. Требование пользователя, не кэш."""
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                conn.execute("DELETE FROM dialogues")
                conn.execute("DELETE FROM turns")
                conn.commit()
            finally:
                conn.close()
            log.info("История очищена")
        except sqlite3.Error as exc:
            log.warning("История: очистить не удалось (%s)", exc)

    def _count(self, table: str) -> int:
        rows = self._read(f"SELECT COUNT(*) AS n FROM {table}")
        return int(rows[0]["n"]) if rows else 0

    def stats(self) -> dict[str, Any]:
        """Сводка для окна истории и трея."""
        rows = self._read(
            "SELECT COUNT(*) AS total,"
            " MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM dialogues")
        row = rows[0] if rows else None
        total = int(row["total"]) if row else 0
        return {
            "total": total,
            "first_ts": (row["first_ts"] if row else None),
            "last_ts": (row["last_ts"] if row else None),
            "size_bytes": (self.path.stat().st_size
                           if self.path.exists() else 0),
            "path": str(self.path),
        }

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> Entry:
        try:
            meta = json.loads(row["meta"] or "{}")
        except (ValueError, TypeError):
            meta = {}
        return Entry(
            id=row["id"], ts=row["ts"], role=row["role"], text=row["text"],
            source=row["source"] or "", confidence=row["confidence"] or 0.0,
            intent=row["intent"] or "", latency_ms=row["latency_ms"] or 0,
            meta=meta if isinstance(meta, dict) else {},
        )


def default_store(keep_days: int = 30, project_root: Path | None = None) -> MemoryStore:
    """Хранилище в стандартном месте проекта."""
    root = project_root or Path(__file__).resolve().parent.parent.parent
    return MemoryStore(root / "data" / "history.db", keep_days=keep_days)