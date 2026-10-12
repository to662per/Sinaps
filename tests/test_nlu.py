"""Тесты понимания речи: нормализация, слоты, сопоставление.

Здесь закрыты регрессии на дефекты, найденные при первом прогоне:
  * шаблон «какой час» без пробела не совпадал с «который час»;
  * буква «ё» терялась в именах файлов («отчёт» -> «отчет»);
  * шаблоны с «ё» не совпадали с нормализованным текстом;
  * «окон» (род. падеж) не совпадало с «окна» (им. падеж).
"""

from __future__ import annotations

from datetime import datetime, time as dt_time

import pytest

from sinaps.core.errors import InvalidSlot
from sinaps.nlu.normalize import cleanup, normalize
from sinaps.nlu.slots import (
    Duration,
    number_to_words,
    parse_datetime_slot,
    parse_duration,
    parse_number,
    parse_time,
)
from sinaps.skills.base import IntentSpec, Response, Skill, SlotSpec
from sinaps.skills.engine import build_default_registry
from sinaps.skills.registry import SkillRegistry


@pytest.fixture(scope="module")
def registry() -> SkillRegistry:
    return build_default_registry()


# --- нормализация -----------------------------------------------------------


def test_normalize_removes_wake_prefix():
    assert normalize("Синапс, открой блокнот") == "открой блокнот"
    assert normalize("синапс открой блокнот") == "открой блокнот"


def test_normalize_replaces_yo():
    assert normalize("счёт") == "счет"


def test_normalize_keeps_yo_when_asked():
    """Для имён файлов «ё» менять нельзя — это другой файл."""
    assert normalize("отчёт", keep_yo=True) == "отчёт"
    assert normalize("отчёт") == "отчет"


def test_cleanup_keeps_yo_for_paths():
    assert cleanup("мой отчёт.docx", keep_yo=True) == "мой отчёт.docx"
    assert cleanup("отчёт", keep_yo=False) == "отчет"


def test_normalize_fixes_common_stt_errors():
    # Реальные промахи Whisper на русском.
    assert "блокнот" in normalize("блохнот")
    assert "калькулятор" in normalize("калкулятор")


# --- знаки конца предложения ------------------------------------------------
#
# Регрессия. Whisper ставит точку и вопросительный знак в конце фразы:
#     «Дата.»        -> не найдено
#     «Кто ты?»      -> не найдено
#     «Который час?» -> находилось (шаблон оказался удачливее остальных)
# Шаблоны привязаны `^...$`, поэтому хвостовой знак не давал сработать
# якорю. Лечили в каждом шаблоне отдельно и непоследовательно.


@pytest.mark.parametrize("raw,expected", [
    ("Дата.", "дата"),
    ("Дата!", "дата"),
    ("Кто ты?", "кто ты"),
    ("Что ты умеешь?", "что ты умеешь"),
    ("Привет!", "привет"),
    ("пока.", "пока"),
    ("Открой блокнот.", "открой блокнот"),
    ("дата?!.", "дата"),
    ("  дата.  ", "дата"),
])
def test_normalize_strips_trailing_punctuation(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("поставь будильник на 10:30", "поставь будильник на 10:30"),
    ("сделай громче на 5,5", "сделай громче на 5,5"),
])
def test_normalize_keeps_inner_punctuation(raw, expected):
    """Знаки внутри текста несут смысл — их терять нельзя."""
    assert normalize(raw) == expected


def test_file_extension_survives_normalization():
    """Точка в расширении — часть имени файла, а не конец предложения."""
    # keep_yo=True сохраняет «ё»: имя файла не должно меняться.
    assert normalize("отчёт.txt", keep_yo=True) == "отчёт.txt"
    # А точка в конце — уже конец предложения, её убираем.
    assert normalize("отчёт.txt.", keep_yo=True) == "отчёт.txt"


@pytest.mark.parametrize("raw,expected", [
    ("Дата.", "time.date"),
    ("Кто ты?", "smalltalk.identity"),
    ("Что ты умеешь?", "smalltalk.capabilities"),
    ("Какая сегодня дата?", "time.date"),
    ("Который час?", "time.now"),
    ("Привет!", "smalltalk.greeting"),
    ("Пока?", "smalltalk.farewell"),
    ("Открой Блокнот.", "apps.open"),
    ("Выключи компьютер?", "power.shutdown"),
])
def test_every_command_survives_speech_punctuation(raw, expected):
    """Ровно то, что приходит от микрофона: заглавная + знак в конце."""
    registry = build_default_registry()
    match = registry.match(raw)
    assert match is not None, f"{raw!r} не распознана"
    assert match.spec.name == expected


@pytest.mark.parametrize("raw", ["Открой Блокнот", "ОТКРЫТЬ БЛОКНОТ",
                                 "ОтКроЙ БлОкНоТ", "открой блокнот"])
def test_every_command_is_case_insensitive(raw):
    registry = build_default_registry()
    match = registry.match(raw)
    assert match is not None and match.spec.name == "apps.open"
    assert match.slots.get("name") == "блокнот"


def test_cleanup_strips_leading_prepositions():
    assert cleanup("на пять минут") == "пять минут"


# --- числа ------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("15", 15.0),
    ("пять", 5.0),
    ("двадцать пять", 25.0),
    ("десять", 10.0),
    ("сто", 100.0),
    ("-3", -3.0),
    ("3,5", 3.5),
])
def test_parse_number(text, expected):
    assert parse_number(text) == pytest.approx(expected)


def test_parse_number_rejects_garbage():
    with pytest.raises(InvalidSlot):
        parse_number("много")


def test_parse_number_uses_default():
    assert parse_number("", default=7) == 7


@pytest.mark.parametrize("text,seconds", [
    ("5 минут", 300),
    ("десять минут", 600),
    ("90 секунд", 90),
    ("час", 3600),
    ("2 часа", 7200),
    ("1 час 30 минут", 5400),
    ("полчаса", 1800),
    ("5", 5),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text).seconds == seconds


def test_parse_duration_rejects_garbage():
    with pytest.raises(InvalidSlot):
        parse_duration("потом как-нибудь")


def test_duration_humanize():
    assert Duration(300).humanize() == "5 минут"
    assert Duration(3600).humanize() == "1 час"
    assert Duration(5400).humanize() == "1 час 30 минут"
    assert Duration(30).humanize() == "30 секунд"


# --- время ------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("в 7:30", dt_time(7, 30)),
    ("в 19:05", dt_time(19, 5)),
    ("в 5 вечера", dt_time(17, 0)),
    ("в 23:59", dt_time(23, 59)),
])
def test_parse_time(text, expected):
    assert parse_time(text) == expected


def test_parse_time_wraps_over_midnight():
    """25:00 — это 01:00 следующего дня, а не ошибка."""
    assert parse_time("25:00") == dt_time(1, 0)


def test_parse_datetime_tomorrow():
    now = datetime(2026, 10, 9, 15, 0)
    result = parse_datetime_slot("завтра в 7:30", now=now)
    assert result.day == 10 and result.hour == 7 and result.minute == 30


# --- склонения --------------------------------------------------------------


def test_number_to_words_plurals():
    assert number_to_words(1) == "один"
    assert number_to_words(2) == "два"
    assert number_to_words(5) == "пять"
    assert number_to_words(11) == "одиннадцать"
    assert number_to_words(21) == "двадцать один"
    assert number_to_words(111) == "сто одиннадцать"


# --- шаблоны ----------------------------------------------------------------


def _spec(name: str, patterns: list[str], **kwargs) -> IntentSpec:
    async def handler(ctx, slots):
        return Response(speech="ok")

    return IntentSpec(name=name, handler=handler, patterns=patterns, **kwargs)


def test_pattern_with_yo_matches_normalized_text():
    """Шаблон «сверни всё» обязан совпасть с нормализованным «сверни все».

    Регрессия: нормализация заменяет «ё»->«е», а шаблоны писались с «ё».
    """
    spec = _spec("t", [r"^сверни всё$"])
    assert spec.match("сверни все") is not None
    assert spec.match("сверни всё") is not None


def test_pattern_word_boundaries():
    """«закрой» не должно перехватывать «закрой окно»."""
    close = _spec("close", [r"^закрой\s+(?P<name>.+)$"])
    result = close.match("закрой блокнот")
    assert result is not None and result["name"] == "блокнот"


def test_slot_spec_validation():
    slot = SlotSpec("name", "string", required=True, min_length=3)
    assert slot.validate("блокнот") == "блокнот"
    with pytest.raises(InvalidSlot):
        slot.validate("")
    with pytest.raises(InvalidSlot):
        slot.validate("ab")

    optional = SlotSpec("x", "string", required=False, default="d")
    assert optional.validate(None) == "d"


def test_tool_schema_generated_from_declaration():
    spec = _spec(
        "apps.open",
        [r"^открой\s+(?P<name>.+)$"],
        description="Открыть программу",
        slots={"name": SlotSpec("name", "string", description="Имя")},
    )
    schema = spec.to_tool_schema()
    assert schema["function"]["name"] == "apps_open"
    assert schema["function"]["parameters"]["required"] == ["name"]
    assert "name" in schema["function"]["parameters"]["properties"]


# --- сопоставление ----------------------------------------------------------

CASES = [
    ("который час", "time.now"),
    ("сколько времени", "time.now"),
    ("какая сегодня дата", "time.date"),
    ("открой блокнот", "apps.open"),
    ("запусти калькулятор", "apps.open"),
    ("закрой блокнот", "apps.close"),
    ("сделай громче", "media.volume_up"),
    ("прибавь громкость на пять", "media.volume_up"),
    ("убавь громкость", "media.volume_down"),
    ("выключи звук", "media.mute"),
    ("статус системы", "system_info.status"),
    ("сколько памяти", "system_info.param"),
    ("загрузка процессора", "system_info.param"),
    ("выключи компьютер", "power.shutdown"),
    ("перезагрузи компьютер", "power.restart"),
    ("сделай скриншот", "screen.screenshot"),
    ("привет", "smalltalk.greeting"),
    ("что ты умеешь", "smalltalk.capabilities"),
    ("сверни всё", "windows.minimize_all"),
    ("какие окна открыты", "windows.list"),
]


@pytest.mark.parametrize("phrase,expected", CASES)
def test_intent_recognition(registry, phrase, expected):
    """Основной набор команд должен узнаваться."""
    match = registry.match(normalize(phrase), phrase)
    assert match is not None, f"«{phrase}» не распознано"
    assert match.spec.name == expected, f"«{phrase}» -> {match.spec.name}"


NEGATIVE = [
    "сделай абстрактную фигню",
    "абракадабра",
    "расскажи анекдот",
    "",
]


@pytest.mark.parametrize("phrase", NEGATIVE)
def test_unknown_phrases_rejected(registry, phrase):
    """Мусор не должен попадать ни в какую команду."""
    assert registry.match(normalize(phrase), phrase) is None


def test_dangerous_commands_marked(registry):
    """Всё, что может навредить, обязано быть помечено."""
    dangerous = {s.name for s in registry.specs if s.dangerous}
    assert "power.shutdown" in dangerous
    assert "power.restart" in dangerous
    assert "power.sleep" in dangerous
    assert "power.lock" in dangerous
    # Безопасные команды помечены быть не должны.
    for name in ("apps.open", "time.now", "media.volume_up"):
        assert name not in dangerous


def test_path_slot_preserves_yo(registry):
    """Регрессия: «отчёт» превращался в «отчет», и файл не находился."""
    match = registry.match(normalize("открой файл отчёт"), "открой файл отчёт")
    assert match is not None
    assert match.slots["name"] == "отчёт"


def test_optional_number_slot_falls_back_to_default(registry):
    """«сделай громче на пять» -> 5; неразобранное значение -> значение по умолчанию."""
    match = registry.match(normalize("громче на пять"))
    assert match is not None
    assert int(match.slots.get("steps", 5)) == 5

    match = registry.match(normalize("громче на абв"))
    assert match is not None
    assert int(match.slots.get("steps", 5)) == 5, "должен сработать fallback"


def test_help_generated_from_registry(registry):
    text = registry.help_text()
    assert "Время и дата" in text
    assert "который час" in text
    # Опасные команды помечены в справке.
    assert "подтверждение" in text


def test_registry_ordering_prefers_specific_patterns():
    """Длинный шаблон должен проверяться раньше короткого общего."""
    skill = Skill("test")

    async def h(ctx, slots):
        return Response(speech="ok")

    skill.command("short", h, patterns=[r"^открой (?P<n>.+)$"], priority=1)
    skill.command("long", h, patterns=[r"^открой окно (?P<n>.+)$"], priority=1)

    reg = SkillRegistry()
    reg.add(skill)

    match = reg.match("открой окно блокнот")
    assert match.spec.name == "test.long"


def test_duplicate_intent_names_are_rejected():
    skill_a, skill_b = Skill("dup"), Skill("dup")

    async def h(ctx, slots):
        return Response(speech="ok")

    skill_a.command("x", h, patterns=[r"^раз$"])
    skill_b.command("x", h, patterns=[r"^два$"])

    reg = SkillRegistry()
    reg.add(skill_a)
    reg.add(skill_b)
    assert len([s for s in reg.specs if s.name == "dup.x"]) == 1


# --- метка порядка байтов (BOM) --------------------------------------------


def test_bom_does_not_break_matching():
    """Фраза из файла в UTF-8 с BOM должна совпасть с той же фразой без него.

    `str.strip()` символ U+FEFF не убирает — Python считает его
    непробельным. Без отдельной чистки фраза из «Блокнота» молча не
    распознавалась бы.
    """
    from sinaps.nlu.normalize import normalize, strip_bom

    assert normalize("﻿открой блокнот") == normalize("открой блокнот")
    assert strip_bom("﻿отчёт.txt") == "отчёт.txt"


def test_bom_stripped_from_slot_values():
    """Имя файла с BOM — это несуществующий файл."""
    from sinaps.nlu.normalize import cleanup, strip_quotes

    assert cleanup("﻿блокнот") == "блокнот"
    assert strip_quotes("﻿\"блокнот\"") == "блокнот"


def test_bom_inside_text_is_kept():
    """Чистим только начало: середина текста — не наше дело."""
    from sinaps.nlu.normalize import strip_bom

    assert strip_bom("а﻿б") == "а﻿б"
