"""Тесты форм глаголов.

Повод: команда «откроем» не распознавалась. Замер на 20 вариантах
показал — работало 7, не работало 13. Для человека это выглядит как
«ассистент то понимает, то нет», хотя дело в одном пропущенном
окончании.

Формы перечислены явно, а не выведены: попытка вывести окончания
дала «откроу» и «запущуь» (у глаголов на -сти формы множественного
числа образуются от другой основы). Для распознавателя выдуманная
форма хуже отсутствующей.
"""

from __future__ import annotations

import re

import pytest

from sinaps.nlu import verbs as V


def _engine():
    from sinaps.core.bus import EventBus
    from sinaps.core.session import SessionController
    from sinaps.skills.engine import SkillEngine, build_default_registry
    from tests.conftest import FakePlatform

    return SkillEngine(registry=build_default_registry(), bus=EventBus(),
                       session=SessionController(EventBus()),
                       platform=FakePlatform())


async def _ask(phrase: str):
    """Фраза — на свежем движке.

    Иначе незакрытое подтверждение опасной команды съедает следующую
    фразу: движок считает её ответом «да или нет».
    """
    return await _engine().respond(phrase, {"source": "voice"})


# --- состав таблицы ---------------------------------------------------------


@pytest.mark.parametrize("lemma", [
    "открыть", "закрыть", "запустить", "включить", "выключить",
    "отключить", "заглушить", "сделать", "увеличить", "уменьшить",
    "добавить", "прибавить", "убрать", "сбавить", "показать",
    "найти", "снимать", "продолжить", "возобновить", "перезагрузить",
    "усыпить", "заблокировать", "изменить", "переключиться",
    "копировать", "запомнить", "свернуть",
])
def test_verb_exists_in_table(lemma):
    assert V.forms_of(lemma), f"глагола {lemma!r} нет в таблице"


def test_infinitive_is_among_forms():
    assert V.forms_of("открыть")[0] is not None
    assert "открыть" in V.forms_of("открыть")


def test_no_invented_endings():
    """Странные окончания — признак неудачной генерации форм."""
    banned = ("ьь", "оу", "еем", "аа", "уь", "оюь")
    for lemma, forms in V._ORDERED.items():
        for form in forms:
            for bad in banned:
                assert bad not in form, (
                    f"{lemma}: подозрительная форма {form!r} ({bad})"
                )


def test_forms_are_unique():
    for lemma, forms in V._ORDERED.items():
        assert len(set(forms)) == len(forms), f"{lemma}: повторяющиеся формы"


def test_forms_sorted_longest_first():
    """Иначе «открыть» перехватит начало «открывайте»."""
    for lemma, forms in V._ORDERED.items():
        lengths = [len(f) for f in forms]
        assert lengths == sorted(lengths, reverse=True), (
            f"{lemma}: формы должны идти от длинных к коротким"
        )


# --- шаблон -----------------------------------------------------------------


def test_pattern_matches_all_faces():
    rx = re.compile(r"^(?:" + V.verb("открыть") + r")\s+(.+)$")

    for phrase in ("открой блокнот", "открыть блокнот", "открою блокнот",
                   "откроем блокнот", "откроешь блокнот",
                   "откроете блокнот", "открывай блокнот",
                   "открывайте блокнот"):
        assert rx.match(phrase), f"{phrase!r} не распознан"


def test_pattern_keeps_object():
    rx = re.compile(r"^(?:" + V.verb("открыть", "запустить")
                    + r")\s+(.+)$")

    match = rx.match("запущу диспетчер задач")
    assert match and match.group(1) == "диспетчер задач"


def test_unknown_verb_used_as_is():
    """Неизвестный глагол не должен исчезнуть молча."""
    pattern = V.verb("выдумка")
    assert "выдумка" in pattern


def test_alt_sorts_longest_first():
    pattern = V.alt("открой", "открывайте")
    assert pattern.index("открывайте") < pattern.index("открой")


def test_verb_with_adds_extra_word():
    pattern = V.verb_with("сведи", "сделать")
    assert "сведи" in pattern
    assert "сделай" in pattern


# --- сквозная проверка через движок ----------------------------------------


@pytest.mark.parametrize("phrase,expect", [
    ("открою блокнот", "блокнот"),
    ("откроем блокнот", "блокнот"),
    ("откроешь блокнот", "блокнот"),
    ("открывайте блокнот", "блокнот"),
    ("запущу блокнот", "блокнот"),
    ("запустим блокнот", "блокнот"),
    ("включим блокнот", "блокнот"),
])
async def test_open_faces_understood(phrase, expect):
    reply = await _ask(phrase)
    assert expect in reply.speech.lower(), (
        f"{phrase!r} -> {reply.speech!r}"
    )


@pytest.mark.parametrize("phrase,expect", [
    ("сделаем громче", "громче"),
    ("сделаю громче", "громче"),
    ("увеличим громче", "громче"),
    ("прибавим громче", "громче"),
    ("уменьшим громкость", "тише"),
])
async def test_volume_faces_understood(phrase, expect):
    reply = await _ask(phrase)
    assert expect in reply.speech.lower(), f"{phrase!r} -> {reply.speech!r}"


@pytest.mark.parametrize("phrase", [
    "выключим компьютер", "выключу компьютер", "выключим всё",
    "перезапустим всё", "перезагрузим компьютер",
    "запри экран", "заблокируем экран", "усыпим компьютер",
])
async def test_power_faces_reach_confirmation(phrase):
    """Опасные команды проверяем по ответу, а не по намерению.

    Они уходят на подтверждение и намерение в ответе не возвращают.
    """
    reply = await _ask(phrase)
    speech = reply.speech.lower()
    assert "подтверди" in speech, f"{phrase!r} -> {reply.speech!r}"
    assert "не понял" not in speech


async def test_screenshot_faces_understood():
    for phrase in ("сделаем скриншот", "снимем скриншот", "сделаю скриншот"):
        reply = await _ask(phrase)
        assert "скриншот" in reply.speech.lower(), f"{phrase!r}"


async def test_existing_commands_not_broken():
    """Расширение форм не должно сломать привычные формулировки."""
    for phrase, expect in (("открой блокнот", "блокнот"),
                           ("сделай громче", "громче"),
                           ("выключи звук", "звук"),
                           ("открывай папку загрузки", "загрузки")):
        reply = await _ask(phrase)
        assert expect in reply.speech.lower(), f"{phrase!r} -> {reply.speech!r}"


async def test_unrelated_phrases_still_rejected():
    """Новые шаблоны не должны становиться слишком широкими.

    Проверяются фразы, которые не команда по смыслу, а не те, где
    глагол подходит, а объекта нет: «открой дверь» — это законная
    команда `apps.open`, просто такой программы не существует.
    """
    for phrase in ("что ты ел на завтрак", "сколько стоит бензин",
                   "хорошая сегодня погода"):
        reply = await _ask(phrase)
        assert "не понял" in reply.speech.lower(), (
            f"{phrase!r} принята как команда: {reply.speech!r}"
        )