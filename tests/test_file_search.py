"""Тесты поиска файлов и папок.

Проверка началась с жалобы «поиск чувствителен к регистру». Замер
показал, что регистр работает верно, а человек описывает три другие
причины, которые выглядят одинаково:

  * требовалось расширение: «отчёт» не находил `Отчёт.docx`;
  * `normalize()` приравнивает «ё» к «е» при разборе фразы, а в имени
    файла остаётся «ё» — `отчет` ≠ `Отчёт`;
  * в ответе не было подсказок, хотя у программ они были.

Проверки идут на временном профиле: реальные пользовательские папки
зависят от машины и делают набор тестов неповторяемым.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sinaps.platform import windows as W


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """Подменяем домашний каталог: поиск идёт только по нему."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for name in ("Desktop", "Documents", "Downloads", "Pictures"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return tmp_path


# --- регистр ----------------------------------------------------------------


@pytest.mark.parametrize("query", [
    "проверка", "Проверка", "ПРОВЕРКА", "пРоВеРкА",
])
def test_case_does_not_matter(profile, query):
    """Регистр в запросе не должен влиять на результат."""
    (profile / "Downloads" / "Проверка.docx").write_text("x")

    found = W._expand_windows_path(query)             # noqa: SLF001
    assert found, f"{query!r} не нашёл файл"
    assert Path(found).name == "Проверка.docx"


# --- «ё» и «е» --------------------------------------------------------------


def test_yo_matches_e(profile):
    """Главный источник ощущения «чувствительности».

    Навык разбирает фразу как `отчет` (нормализация ради сопоставления
    с командами), а на диске лежит `Отчёт`. Без приравнивания файл
    не находится никогда.
    """
    (profile / "Downloads" / "Отчёт.docx").write_text("x")

    assert W._expand_windows_path("отчет")            # noqa: SLF001
    assert W._expand_windows_path("Отчет")            # noqa: SLF001


def test_e_does_not_match_yo_file(profile):
    """Проверка в обе стороны: и «ё» находится по «е», и наоборот."""
    (profile / "Downloads" / "Отчет.docx").write_text("x")
    assert W._expand_windows_path("отчёт")            # noqa: SLF001


# --- расширение -------------------------------------------------------------


def test_extension_is_not_required(profile):
    """Человек говорит «отчёт», а не «отчёт docx».

    Требовать расширение — значит требовать то, чего он не произносил.
    """
    (profile / "Downloads" / "Отчёт.docx").write_text("x")

    found = W._expand_windows_path("отчёт")           # noqa: SLF001
    assert found and Path(found).suffix == ".docx"


def test_full_name_still_found(profile):
    """С расширением тоже должно работать — это не должно сломаться."""
    (profile / "Downloads" / "Отчёт.docx").write_text("x")
    assert W._expand_windows_path("отчёт.docx")      # noqa: SLF001


def test_exact_match_wins_over_stem(profile):
    """«1.pdf» не должен открываться вместо «1.txt».

    Сравнение идёт и по полному имени, и без расширения, но полное
    проверяется раньше.
    """
    (profile / "Documents" / "1.txt").write_text("x")
    (profile / "Downloads" / "1.pdf").write_text("x")

    found = W._expand_windows_path("1.pdf")           # noqa: SLF001
    assert found and Path(found).suffix == ".pdf"


# --- подсказки --------------------------------------------------------------


def test_suggest_finds_close_name(profile):
    (profile / "Downloads" / "Смета.XLSX").write_text("x")

    assert "Смета.XLSX" in W.suggest_paths("смету")   # noqa: SLF001


def test_suggest_empty_for_nonsense(profile):
    """Мусор не должен порождать подсказки."""
    (profile / "Downloads" / "Смета.XLSX").write_text("x")

    assert W.suggest_paths("ывафыва") == []           # noqa: SLF001


def test_suggest_respects_threshold(profile):
    """Порог не пропускает случайные совпадения.

    Проверено вручную: на 0,66 «отчет» предлагал «тест.txt», и
    человек открыл бы не тот файл. Ложная подсказка хуже её
    отсутствия — отсутствие заметно, ошибка нет.
    """
    (profile / "Downloads" / "тест.txt").write_text("x")

    assert "тест.txt" not in W.suggest_paths("отчет")  # noqa: SLF001


def test_suggest_respects_limit(profile):
    for i in range(6):
        (profile / "Downloads" / f"смета{i}.xlsx").write_text("x")

    assert len(W.suggest_paths("смет", limit=3)) <= 3  # noqa: SLF001


# --- ничего лишнего не открывается ------------------------------------------


def test_unknown_name_returns_none(profile):
    """Без нечёткого автооткрытия: нечётное имя не должно открываться."""
    (profile / "Downloads" / "Отчёт2026.pdf").write_text("x")

    # Точное имя без года неоднозначно — открывать нельзя.
    assert W._expand_windows_path("отчет") is None      # noqa: SLF001


def test_fuzzy_is_suggestion_not_action(profile):
    """Ключевой принцип проекта: нечёткое совпадение не выполняет действие.

    Иначе «открой файл отчет» открыл бы «Отчёт2026.pdf» молча, а
    человек бы не заметил — и смотрел бы чужой документ.
    """
    (profile / "Downloads" / "Отчёт2026.pdf").write_text("x")

    assert W._expand_windows_path("отчет") is None      # noqa: SLF001
    assert W.suggest_paths("отчет") == []               # noqa: SLF001


# --- startfile не должен ронять навык ---------------------------------------


def test_open_path_survives_startfile_error(profile, monkeypatch):
    """Windows показывает диалог выбора приложения; если его закрыли —
    `os.startfile` бросает. Навык должен сказать «не удалось», а не
    «Что-то сломалось»."""
    target = profile / "Downloads" / "Отчёт.docx"
    target.write_text("x")

    def boom(_path):
        raise OSError(1223, "Операция была отменена пользователем")

    monkeypatch.setattr(W.os, "startfile", boom)

    platform = W.WindowsPlatform()
    assert platform.open_path(str(target)) is False


def test_open_path_reports_missing(profile):
    platform = W.WindowsPlatform()
    assert platform.open_path("несуществующее-имя") is False


# --- прочее -----------------------------------------------------------------


def test_user_roots_follow_profile(profile):
    """Поиск ходит в профиль пользователя, а не куда попало."""
    roots = W._user_roots()                           # noqa: SLF001
    assert roots, "список корней не должен быть пустым"
    assert all(str(root).startswith(str(profile)) for root in roots)


def test_folders_are_found_too(profile):
    """Папки ищутся так же, как файлы.

    Имя папки нужно называть точно: «Архив 2024» не находится по
    запросу «архив» — нечёткое совпадение не открывает файлы молча.
    """
    folder = profile / "Downloads" / "Архив 2024"
    folder.mkdir()

    found = W._expand_windows_path("архив 2024")      # noqa: SLF001
    assert found and Path(found).is_dir()


def test_quotes_are_stripped(profile):
    (profile / "Downloads" / "Отчёт.docx").write_text("x")

    assert W._expand_windows_path('"отчёт"')          # noqa: SLF001