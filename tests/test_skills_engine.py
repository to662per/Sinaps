"""Тесты движка понимания: полный путь от реплики до действия.

Главный акцент — на безопасности. Тесты проверяют, что опасное действие
НЕ выполняется без явного подтверждения, и что при отказе, невнятном
ответе или истечении времени платформа не вызывается вообще.

Заглушка платформы — в `conftest.py` (фикстура `platform_stub`).
"""

from __future__ import annotations

import time

import pytest

from sinaps.skills.engine import SkillEngine, build_default_registry


@pytest.fixture
def engine(platform_stub):
    return SkillEngine(build_default_registry(), platform=platform_stub,
                       user_name="Капитан")


async def say(engine: SkillEngine, phrase: str, source: str = "text"):
    return await engine.respond(phrase, {"source": source, "history": []})


# --- безопасные команды -----------------------------------------------------


async def test_safe_command_executes(engine, platform_stub):
    reply = await say(engine, "открой блокнот")
    assert "Открываю" in reply.speech
    assert platform_stub.calls == ["launch:блокнот"]


async def test_time_answer(engine, platform_stub):
    reply = await say(engine, "который час")
    assert "Сейчас" in reply.speech
    assert platform_stub.calls == []


async def test_system_status(engine, platform_stub):
    reply = await say(engine, "статус системы")
    assert "процессор" in reply.speech
    assert "память" in reply.speech


async def test_status_uptime_is_sane(engine, platform_stub):
    """Регрессия: uptime считался как «74573543 дня».

    Причина — деление `psutil.boot_time()` (секунды) на 1000.
    Здесь проверяем верхнюю границу: разумная машина работает не дольше
    нескольких лет подряд.
    """
    from sinaps.platform.windows import WindowsPlatform

    status = WindowsPlatform().status()
    if status.uptime_hours is None:
        pytest.skip("uptime недоступен")
    assert 0 <= status.uptime_hours < 24 * 365 * 10, (
        f"нереальное время работы: {status.uptime_hours} часов"
    )


async def test_volume_up_uses_percent(engine, platform_stub):
    await say(engine, "прибавь громкость на три")
    assert platform_stub.calls == ["volume_up:3"]


async def test_volume_down_uses_percent(engine, platform_stub):
    await say(engine, "сделай тише на два")
    assert platform_stub.calls == ["volume_down:2"]


async def test_volume_up_default_percent(engine, platform_stub):
    """Без названного числа — 10 процентов.

    Раньше здесь стояло 5: это было число нажатий клавиши, каждое из
    которых сдвигает уровень на 2 процента, и голос объявлял «5» при
    фактических 10. Теперь единица одна — проценты.
    """
    await say(engine, "сделай громче")
    assert platform_stub.calls == ["volume_up:10"]


async def test_volume_up_reports_measured_level(engine, platform_stub):
    """Ответ обязан совпадать с тем, что реально стало.

    Раньше навык объявлял величину сдвига, а не результат: человек
    слышал «5» и видел, что громкость уехала на 10 процентов, и
    решал, что ассистент считает неверно. Теперь результат измерен.
    """
    reply = await say(engine, "сделай громче на десять")
    assert platform_stub.volume_percent == 60
    assert "60" in reply.speech


async def test_volume_down_reports_measured_level(engine, platform_stub):
    reply = await say(engine, "сделай тише на десять")
    assert platform_stub.volume_percent == 40
    assert "40" in reply.speech


async def test_volume_up_at_maximum_says_already(engine, platform_stub):
    """У края диапазона нельзя обещать сдвиг, которого не будет."""
    platform_stub.volume_percent = 100
    reply = await say(engine, "сделай громче на двадцать")
    assert "уже 100" in reply.speech


async def test_volume_up_clamps_at_edge(engine, platform_stub):
    """Просили больше, чем оставалось, — отвечаем о фактическом итоге."""
    platform_stub.volume_percent = 90
    reply = await say(engine, "сделай громче на тридцать")
    assert platform_stub.volume_percent == 100
    assert "100" in reply.speech


@pytest.mark.parametrize("phrase,expected", [
    ("поставь громкость на 50", 50),
    ("поставь громкость на сто", 100),
    ("сделай громкость 30", 30),
    ("выставь громкость на семьдесят", 70),
    ("установим громкость на 60", 60),
    ("громкость на 35 процентов", 35),
    ("громкость 40", 40),
    ("поставь громкость на 0", 0),
])
async def test_volume_set_is_exact(engine, platform_stub, phrase, expected):
    """«Поставь громкость на 50» — это ровно 50, а не «увеличь на 50».

    Отдельная команда, потому что сдвигом это не сделать: из 80 в 50
    пришлось бы угадывать число нажатий, и результат зависел бы от
    того, где громкость стояла в момент команды.
    """
    platform_stub.volume_percent = 80

    reply = await say(engine, phrase)

    assert platform_stub.volume_percent == expected, (
        f"{phrase!r} -> {platform_stub.volume_percent}, ждали {expected}"
    )
    assert "не понял" not in reply.speech.lower()


async def test_volume_set_zero_is_not_treated_as_missing(engine, platform_stub):
    """Ноль — полноценное значение, а не «слот не заполнен».

    Раньше `has_missing_slots` проверял `not match.slots.get(...)`,
    и ноль считался пустым: команда доходила до навыка с правильным
    числом и отвечала «нужно percent».
    """
    reply = await say(engine, "поставь громкость на 0")

    assert platform_stub.volume_percent == 0
    assert "нужно" not in reply.speech.lower()


async def test_volume_set_rejects_out_of_range(engine, platform_stub):
    platform_stub.volume_percent = 50

    reply = await say(engine, "поставь громкость на 150")

    assert platform_stub.volume_percent == 50, "уровень не тронут"
    assert "150" not in reply.speech


async def test_volume_shift_and_set_do_not_mix(engine, platform_stub):
    """«сделай громкость 30» ставит, «сделай громче на 20» сдвигает."""
    await say(engine, "поставь громкость на 50")

    await say(engine, "сделай громче на 20")
    assert platform_stub.volume_percent == 70

    await say(engine, "сделай громкость 30")
    assert platform_stub.volume_percent == 30


async def test_volume_level_reports_percent(engine, platform_stub):
    """Пункт «точная громкость»: навык з��ания уровня."""
    platform_stub.volume_percent = 37
    reply = await say(engine, "какая громкость")
    assert "37" in reply.speech


# --- опасные команды: главное ----------------------------------------------


@pytest.mark.parametrize("phrase", [
    "выключи компьютер",
    "перезагрузи компьютер",
    "усыпи компьютер",
    "заблокируй экран",
])
async def test_dangerous_command_asks_and_does_not_execute(engine, platform_stub, phrase):
    """Опасная команда обязана спросить и НЕ выполниться сразу."""
    reply = await say(engine, phrase)

    assert "да или нет" in reply.speech, reply.speech
    assert platform_stub.calls == [], (
        f"действие выполнено без подтверждения: {platform_stub.calls}"
    )
    assert engine.has_pending is True


async def test_confirmation_yes_executes(engine, platform_stub):
    await say(engine, "выключи компьютер")
    platform_stub.calls.clear()

    reply = await say(engine, "да")
    assert "Выключаю" in reply.speech
    assert platform_stub.calls == ["power:shutdown:20"], (
        "ожидалась задержка, чтобы пользователь успел отменить"
    )


@pytest.mark.parametrize("refusal", ["нет", "не", "отмена", "стоп", "не надо"])
async def test_confirmation_refusal_blocks_execution(engine, platform_stub, refusal):
    await say(engine, "выключи компьютер")
    platform_stub.calls.clear()

    reply = await say(engine, refusal)
    assert "Отмен" in reply.speech
    assert platform_stub.calls == [], "отказ должен блокировать действие"
    assert engine.has_pending is False


@pytest.mark.parametrize("vague", ["хм", "ну", "а", "что"])
async def test_unclear_confirmation_blocks_execution(engine, platform_stub, vague):
    """Нечёткий ответ не должен считаться согласием."""
    await say(engine, "выключи компьютер")
    platform_stub.calls.clear()

    reply = await say(engine, vague)
    assert platform_stub.calls == [], "неясность не должна приводить к действию"
    assert "подтверждаешь" in reply.speech.lower() or "не понял" in reply.speech.lower()
    assert engine.has_pending is False


async def test_confirmation_expires(engine, platform_stub):
    await say(engine, "выключи компьютер")
    platform_stub.calls.clear()

    # Подделываем давнее время: ждать 15 секунд в тесте незачем.
    engine._pending.created_at = time.time() - engine.confirm_ttl - 1

    reply = await say(engine, "да")
    assert platform_stub.calls == [], "просроченное подтверждение не должно выполнять действие"
    assert "вышло" in reply.speech.lower()


async def test_confirmation_is_single_use(engine, platform_stub):
    """Подтверждение нельзя использовать повторно."""
    await say(engine, "выключи компьютер")
    await say(engine, "да")
    platform_stub.calls.clear()

    await say(engine, "да")
    assert platform_stub.calls == [], "старое подтверждение не должно срабатывать снова"


async def test_new_command_replaces_pending(engine, platform_stub):
    """Новая команда сбрасывает ожидающее подтверждение."""
    await say(engine, "выключи компьютер")
    assert engine.has_pending is True

    await say(engine, "который час")
    assert engine.has_pending is False, "обычная команда должна сбросить подтверждение"
    assert platform_stub.calls == [], "отменённое действие не должно выполняться"


# --- неизвестные команды ----------------------------------------------------


async def test_unknown_command_is_honest(engine, platform_stub):
    reply = await say(engine, "сделай абстрактную фигню")
    assert "Не понял" in reply.speech
    assert platform_stub.calls == [], "непонятная команда не должна ничего делать"


async def test_unknown_command_offers_help(engine):
    reply = await say(engine, "абракадабра")
    assert "умеешь" in (reply.follow_up or "") or "умеешь" in reply.speech


async def test_capabilities_lists_commands(engine):
    reply = await say(engine, "что ты умеешь")
    assert "умею" in reply.speech.lower()
    assert reply.display and "Время и дата" in reply.display


# --- устойчивость -----------------------------------------------------------


async def test_engine_never_raises(engine, platform_stub):
    """Движок не должен бросать исключения в конвейер."""
    for phrase in ["", "   ", "?!", "а" * 500, "выключи компьютер", "да"]:
        reply = await say(engine, phrase)
        assert reply.speech, f"пустой ответ на {phrase!r}"


async def test_yes_no_detection_covers_russian_forms():
    from sinaps.core.context import ContextFactory

    factory = ContextFactory()
    for yes in ["да", "ага", "ок", "хорошо", "подтверждаю", "согласен"]:
        assert factory.build(yes).is_yes(yes), yes
    for no in ["нет", "не", "отмена", "стоп", "не надо"]:
        assert factory.build(no).is_no(no), no


async def test_greeting_uses_user_name(engine):
    reply = await say(engine, "привет")
    assert "Капитан" in reply.speech


async def test_voice_and_text_get_different_not_found_messages(engine):
    voice = await say(engine, "абракадабра", source="voice")
    text = await say(engine, "абракадабра", source="text")
    assert voice.speech != text.speech, "для голоса и текста нужны разные подсказки"
