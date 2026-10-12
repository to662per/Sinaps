"""Тесты хранилища истории.

История — то, к чему у пользователя нет привычки, поэтому ошибки здесь
незаметны сразу: окно открылось, база пустая, и понять нечем. Тесты
проверяют именно то, что не видно снаружи — целостность записей,
потокобезопасность и поведение при поломке БД.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sinaps.memory.store import Entry, MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "history.db", keep_days=30)


# --- запись и чтение -------------------------------------------------------


def test_add_and_read_dialogue(store):
    entry_id = store.add_dialogue("user", "открой блокнот", source="voice",
                                  confidence=0.87, intent="apps.open")
    assert entry_id is not None

    entries = store.recent(limit=10)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, Entry)
    assert entry.role == "user"
    assert entry.text == "открой блокнот"
    assert entry.source == "voice"
    assert entry.confidence == pytest.approx(0.87)
    assert entry.intent == "apps.open"


def test_recent_is_newest_first(store):
    for i in range(5):
        store.add_dialogue("user", f"реплика {i}")
    texts = [e.text for e in store.recent(limit=10)]
    assert texts == ["реплика 4", "реплика 3", "реплика 2", "реплика 1",
                     "реплика 0"]


def test_filter_by_role(store):
    store.add_dialogue("user", "вопрос")
    store.add_dialogue("assistant", "ответ")

    assert len(store.recent(role="user")) == 1
    assert store.recent(role="user")[0].text == "вопрос"
    assert len(store.recent(role="assistant")) == 1


def test_empty_text_is_not_stored(store):
    """Пустая реплика — мусор в истории, а не запись."""
    assert store.add_dialogue("user", "") is None
    assert store.add_dialogue("user", "   ") is None
    assert store.recent() == []


def test_unicode_is_stored_as_is(store):
    """«ё» и кириллица должны пережить round-trip."""
    store.add_dialogue("user", "отчёт за 2026 год")
    assert store.recent()[0].text == "отчёт за 2026 год"


def test_bom_is_stripped_from_history(store):
    """BOM в истории — невидимый мусор.

    Он приезжает из файлов в кодировке UTF-8 с BOM и из конвейера
    PowerShell. `str.strip()` его не убирает: в Python символ U+FEFF
    не считается пробельным. В поиске такой реплики не найти, а при
    экспорте она ломает разбор.
    """
    store.add_dialogue("user", "﻿который час")
    store.add_turn("﻿открой блокнот", "Открываю")

    assert store.recent()[0].text == "который час"
    assert store.recent_turns()[0].user_text == "открой блокнот"


def test_meta_survives_roundtrip(store):
    store.add_dialogue("user", "ок", meta={"мс": 123, "модель": "turbo"})
    meta = store.recent()[0].meta
    assert meta == {"мс": 123, "модель": "turbo"}


def test_broken_meta_does_not_break_reading(store):
    """Испорченный JSON не должен ронять чтение истории."""
    store.add_dialogue("user", "нормальная")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE dialogues SET meta = '{{{битое' WHERE id = 1")
        conn.commit()

    entries = store.recent()
    assert len(entries) == 1
    assert entries[0].meta == {}, "битый meta читается как пустой словарь"


# --- поиск -----------------------------------------------------------------


def test_search_finds_substring(store):
    store.add_dialogue("user", "открой блокнот")
    store.add_dialogue("user", "закрой блокнот")
    store.add_dialogue("user", "который час")

    found = store.search("блокнот")
    assert len(found) == 2
    assert all("блокнот" in e.text for e in found)


def test_search_with_nothing_found(store):
    store.add_dialogue("user", "открой блокнот")
    assert store.search("блокнота") == []


# --- обмены ----------------------------------------------------------------


def test_turn_is_saved_with_response(store):
    turn_id = store.add_turn("открой блокнот", "Открываю блокнот",
                             intent="apps.open", source="voice", total_ms=480)
    assert turn_id is not None

    turns = store.recent_turns(limit=5)
    assert len(turns) == 1
    assert turns[0].user_text == "открой блокнот"
    assert turns[0].bot_text == "Открываю блокнот"
    assert turns[0].intent == "apps.open"


def test_turn_without_user_text_is_skipped(store):
    assert store.add_turn("   ", "ответ") is None


# --- факты -----------------------------------------------------------------


def test_facts_roundtrip(store):
    store.set_fact("user_name", "Капитан")
    assert store.get_fact("user_name") == "Капитан"


def test_fact_overwritten_not_duplicated(store):
    store.set_fact("city", "Москва")
    store.set_fact("city", "Казань")

    assert store.get_fact("city") == "Казань"
    assert len(store.all_facts()) == 1, "факт должен обновляться, а не плодиться"


def test_missing_fact_returns_default(store):
    assert store.get_fact("нет_такого", "запасное") == "запасное"


# --- обслуживание ----------------------------------------------------------


def test_prune_removes_old_entries(tmp_path):
    store = MemoryStore(tmp_path / "history.db", keep_days=1)
    old = time.time() - 5 * 86400

    store.add_dialogue("user", "старое", ts=old)
    store.add_dialogue("user", "свежее")

    assert len(store.recent()) == 2
    removed = store.prune()
    assert removed == 1
    texts = [e.text for e in store.recent()]
    assert texts == ["свежее"]


def test_prune_keeps_everything_when_keep_days_zero(tmp_path):
    store = MemoryStore(tmp_path / "history.db", keep_days=0)
    store.add_dialogue("user", "что угодно", ts=time.time() - 10 * 86400)

    assert store.prune() == 0
    assert len(store.recent()) == 1


def test_clear_wipes_history(store):
    store.add_dialogue("user", "раз")
    store.add_dialogue("user", "два")
    store.add_turn("раз", "ответ")

    store.clear()

    assert store.recent() == []
    assert store.recent_turns() == []


def test_facts_survive_clear(store):
    """Очистка истории не должна стирать настройки пользователя."""
    store.set_fact("user_name", "Капитан")
    store.add_dialogue("user", "раз")

    store.clear()

    assert store.get_fact("user_name") == "Капитан"


def test_stats(store):
    store.add_dialogue("user", "раз")
    store.add_dialogue("assistant", "два")

    stats = store.stats()
    assert stats["total"] == 2
    assert stats["size_bytes"] > 0
    assert stats["first_ts"] is not None


def test_by_day_buckets(store):
    store.add_dialogue("user", "сегодня")
    buckets = store.by_day(days=7)
    assert len(buckets) >= 1
    assert sum(buckets.values()) == 1


# --- живучесть -------------------------------------------------------------


def test_broken_database_does_not_raise(store):
    """Битая БД не должна ронять ассистент.

    Главное требование к памяти: ошибка хранилища не превращается в
    молчание. Пользователь не должен терять голос из-за того, что
    файл истории заперт другим процессом.
    """
    store.add_dialogue("user", "раз")   # создаём файл

    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TABLE dialogues")
        conn.commit()

    # Ни запись, ни чтение не должны бросать наружу.
    assert store.add_dialogue("user", "ещё") is None or True
    assert isinstance(store.recent(), list)
    assert isinstance(store.stats(), dict)


def test_unwritable_path_is_survivable(tmp_path):
    """Путь, который невозможно создать, — не повод ронять программу.

    Каталог хранилище создаёт само, поэтому обычный «нет такого каталога»
    не мешает. Мешает файл на месте каталога: `mkdir` на таком пути
    всегда падает.
    """
    blocker = tmp_path / "файл_на_месте_каталога"
    blocker.write_text("я не каталог", encoding="utf-8")

    store = MemoryStore(blocker / "history.db", keep_days=30)

    assert store.add_dialogue("user", "ок") is None
    assert store.recent() == []
    assert store.stats()["total"] == 0


def test_store_creates_missing_directory(tmp_path):
    """Отсутствующий каталог создаётся сам — это удобство, а не ошибка."""
    target = tmp_path / "data" / "logs" / "history.db"
    store = MemoryStore(target, keep_days=30)

    assert store.add_dialogue("user", "ок") is not None
    assert target.exists()


def test_concurrent_writes_from_threads(store):
    """Запись идёт из разных потоков — потеря записей недопустима."""
    errors: list[Exception] = []

    def writer(worker: int) -> None:
        try:
            for i in range(20):
                store.add_dialogue("user", f"worker {worker} msg {i}")
        except Exception as exc:      # pragma: no cover - проверяем отсутствие
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"потоки падали: {errors}"
    assert len(store.recent(limit=200)) == 80


def test_reopen_after_close(tmp_path):
    """Хранилище должно переживать перезапуск программы."""
    path = tmp_path / "history.db"

    first = MemoryStore(path, keep_days=30)
    first.add_dialogue("user", "до перезапуска")

    second = MemoryStore(path, keep_days=30)
    assert second.recent()[0].text == "до перезапуска"