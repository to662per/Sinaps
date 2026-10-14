"""Тесты панели возможностей и таксономии.

Проверяем не рисунок, а обещания. Панель — единственное место, где
ассистент показывает человеку, что он умеет. Ошибка здесь двоякая:
лишняя карточка (обещаем несуществующее — ровно тот баг, который
чинили) и недостающая (человек не знает, что умеет).

Поэтому самые важные тесты — отрицательные: панель обязана молчать
о том, чего в реестре нет.
"""

from __future__ import annotations

import os

import pytest

from sinaps.skills import taxonomy
from sinaps.ui import panel_data as P

pytest.importorskip("PySide6", reason="PySide6 не установлен")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture(scope="module")
def registry():
    from sinaps.skills.engine import build_default_registry

    return build_default_registry()


@pytest.fixture(scope="module")
def data(registry):
    return P.build_cards(registry.specs)


# --- таксономия -------------------------------------------------------------


def test_group_of_splits_intent():
    assert taxonomy.group_of("media.volume_up") == "media"
    assert taxonomy.group_of("time") == "time"


def test_every_registered_group_is_described(registry):
    """Группа без описания не покажется — и человек её не найдёт.

    Лучше заметный пропуск, чем вкладка «unknown».
    """
    missing = sorted({
        taxonomy.group_of(spec.name)
        for spec in registry.specs
        if not spec.hidden
    } - set(taxonomy.known_groups()))
    assert not missing, f"группы без описания в таксономии: {missing}"


def test_taxonomy_has_title_and_icon():
    for key in taxonomy.known_groups():
        group = taxonomy.describe(key)
        assert group is not None
        assert group.title, f"{key}: нет названия"
        assert group.icon, f"{key}: нет значка"


def test_taxonomy_voice_words_never_empty():
    """Пустой список слов означает, что группу нельзя назвать голосом."""
    for key in taxonomy.known_groups():
        group = taxonomy.describe(key)
        assert group is not None
        assert group.voice, f"{key}: нечем назвать вслух"


def test_speech_names_are_human():
    assert taxonomy.speech_names(["media", "apps"]) == "звук, программы"


def test_speech_names_ignores_unknown():
    assert taxonomy.speech_names(["нет_такой"]) == ""


def test_speech_names_respects_limit():
    """Девять названий голосом — двадцать секунд и ни одного запоминаемого."""
    many = taxonomy.known_groups()[:8]
    assert len(taxonomy.speech_names(many, limit=3).split(", ")) == 3


# --- сборка карточек --------------------------------------------------------


def test_every_visible_command_gets_a_card(data, registry):
    visible = {s.name for s in registry.specs if not s.hidden}
    shown = {c.intent for cards in data.cards.values() for c in cards}
    assert shown == visible, "раздел не совпадает с реестром"


def test_panel_shows_no_invented_commands(data):
    """Ключевой тест: панель не вправе обещать то, чего нет.

    Именно этим был сломан «что ты умеешь»: обещал экран, которого нет.
    Та же ошибка на карточках была бы наряднее, но осталась бы ошибкой.
    """
    names = set()
    for cards in data.cards.values():
        names.update(c.intent for c in cards)

    for invented in ("timer.set", "weather.today", "notes.create",
                     "smart_home.light"):
        assert invented not in names, f"панель обещает несуществующее: {invented}"


def test_every_card_has_a_phrase(data):
    """Карточка без примера фразы бесполезна: человек не знает, что сказать."""
    for cards in data.cards.values():
        for card in cards:
            assert card.phrase.strip(), f"{card.intent}: нет примера фразы"


def test_card_titles_are_unique_per_group(data):
    """Одинаковые заголовки делают раздел неразличимым.

    Так было, когда длинные описания откатывались на название группы:
    в «Звук и музыка» появлялись две карточки «Звук и музыка».
    """
    for key, cards in data.cards.items():
        titles = [c.title for c in cards]
        assert len(set(titles)) == len(titles), (
            f"в разделе {key!r} повторяются заголовки: {titles}"
        )


def test_long_description_is_not_dropped(registry):
    """Обрезание описания тихо превращало бы разные команды в одинаковые."""
    spec = next(s for s in registry.specs if s.name == "media.play_pause")
    card = next(c for cards in P.build_cards([spec]).cards.values()
                for c in cards)
    assert card.title == spec.description
    assert len(card.title) > 28, "описание длинное — оно не должно теряться"


def test_hidden_commands_are_not_shown(registry):
    hidden = [s for s in registry.specs if s.hidden]
    if not hidden:
        pytest.skip("скрытых команд нет")
    built = P.build_cards(registry.specs)
    shown = {c.intent for cards in built.cards.values() for c in cards}
    for spec in hidden:
        assert spec.name not in shown


def test_unknown_group_is_skipped(data):
    names = {c.intent for cards in data.cards.values() for c in cards}
    assert "секретная.команда" not in names


def test_card_strips_quotes(registry):
    spec = next(s for s in registry.specs if s.name == "apps.open")
    card = next(c for cards in P.build_cards([spec]).cards.values() for c in cards)
    assert not card.safe_phrase.startswith(('"', "«"))


def test_total_matches_cards(data):
    assert data.total == sum(len(c) for c in data.cards.values())


def test_group_order_follows_taxonomy(data):
    """Порядок вкладок задаёт человек в таксономии, а не сортировка."""
    declared = [k for k in taxonomy.known_groups() if k in data.cards]
    assert [key for key, _t, _c in data.groups] == declared


# --- чипы -------------------------------------------------------------------


def test_chips_never_include_dangerous_commands(data):
    """Кнопка «выключи компьютер» среди подсказок — плохая идея.

    Подтверждение спасёт, но просить об этом кнопкой незачем.
    """
    for chip in data.chips:
        assert not chip.dangerous, f"опасная команда в чипах: {chip.intent}"


def test_chips_are_phrase_ready(data):
    for chip in data.chips:
        assert chip.safe_phrase.strip(), f"{chip.intent}: чип без фразы"


def test_chips_are_bounded(data):
    assert 0 < len(data.chips) <= P.CHIP_COUNT


def test_chips_start_from_frequent_groups(data):
    """Первый чип должен быть из самой ходовой группы, а не случайной."""
    if data.chips:
        assert data.chips[0].group == P._CHIP_GROUPS[0]


def test_power_group_is_absent_from_chips(data):
    """Все команды питания опасные — значит, в ленте их быть не может."""
    assert "power" not in P._CHIP_GROUPS


# --- контекстные чипы -------------------------------------------------------


def test_context_chips_are_same_group(data):
    picked = P.chips_for_intent(data, "media.volume_up")
    assert picked, "после громкости должно быть что предложить"
    assert all(c.group == "media" for c in picked)


def test_context_chips_exclude_the_command_itself(data):
    picked = P.chips_for_intent(data, "media.volume_up")
    assert all(c.intent != "media.volume_up" for c in picked)


def test_context_chips_respect_limit(data):
    assert len(P.chips_for_intent(data, "media.mute", limit=2)) <= 2


def test_context_chips_for_unknown_command(data):
    """Чужое намерение не должно ломать выдачу."""
    assert isinstance(P.chips_for_intent(data, "нет.такой"), list)


def test_context_chips_empty_for_last_in_group(data):
    cards = data.cards["windows"]
    picked = P.chips_for_intent(data, cards[-1].intent, limit=99)
    assert all(c.intent != cards[-1].intent for c in picked)


# --- виджет -----------------------------------------------------------------


def test_panel_builds_without_data(qapp):
    """Пустая панель обязана создаваться: иначе трей упадёт при старте."""
    from sinaps.ui.panel import Panel

    panel = Panel()
    assert panel is not None


def test_panel_does_not_take_focus(qapp, data):
    """Условие работоспособности: окно не должно забирать фокус.

    Иначе открытие панели выключает микрофон ровно в момент, когда
    человек пытается что-то сказать.
    """
    from PySide6.QtCore import Qt

    from sinaps.ui.panel import Panel

    panel = Panel()
    panel.set_data(data)
    panel.show()
    qapp.processEvents()

    flags = panel.windowFlags()
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus, (
        "панель заберёт фокус и сломает микрофон"
    )
    assert flags & Qt.WindowType.WindowStaysOnTopHint, (
        "панель должна быть поверх других окон"
    )
    assert flags & Qt.WindowType.FramelessWindowHint, (
        "системная рамка выглядит чужеродно на полупрозрачной подложке"
    )


def test_panel_shows_group_and_reports_choice(qapp, data):
    from sinaps.ui.panel import Panel

    panel = Panel()
    chosen: list[str] = []
    panel.phrase_chosen.connect(chosen.append)

    panel.set_data(data)
    qapp.processEvents()
    panel._choose("сделай громче")          # noqa: SLF001

    assert chosen == ["сделай громче"]


def test_panel_idle_timer_is_armed(qapp, data):
    """Таймер бездействия обязан быть запущен сразу, иначе панель
    никогда не закроется сама."""
    from sinaps.ui.panel import Panel

    panel = Panel()
    panel.set_data(data)
    qapp.processEvents()
    assert panel._idle.isActive()          # noqa: SLF001


def test_panel_action_resets_idle_timer(qapp, data):
    """Любое действие человека перезапускает отсчёт."""
    from sinaps.ui.panel import Panel

    panel = Panel()
    panel.set_data(data)
    qapp.processEvents()

    panel._idle.stop()                     # noqa: SLF001
    assert not panel._idle.isActive()      # noqa: SLF001

    panel._choose("который час")           # noqa: SLF001
    assert panel._idle.isActive(), "действие должно перезапустить таймер"


def test_panel_tab_switch_resets_idle_timer(qapp, data):
    """Переключение вкладки — тоже работа с панелью."""
    from sinaps.ui.panel import Panel

    panel = Panel()
    panel.set_data(data)
    qapp.processEvents()

    panel._idle.stop()                     # noqa: SLF001
    panel.show_group("media")              # noqa: SLF001
    assert panel._idle.isActive()          # noqa: SLF001


def test_panel_idle_close_is_configured(qapp, data):
    """Человек решил: 25 секунд. Проверяем, что так и есть."""
    from sinaps.ui.panel import Panel

    panel = Panel()
    assert panel._idle.interval() == int(P.IDLE_CLOSE_S * 1000)   # noqa: SLF001
    assert P.IDLE_CLOSE_S == 25.0


def test_panel_idle_emits_and_closes(qapp, data):
    from sinaps.ui.panel import Panel

    panel = Panel()
    closed: list[int] = []
    panel.idle_closed.connect(lambda: closed.append(1))

    panel.set_data(data)
    panel.show()
    qapp.processEvents()

    panel._on_idle()                       # noqa: SLF001
    qapp.processEvents()

    assert closed == [1], "о бездействии должна сообщаться причина закрытия"
    assert not panel.isVisible()


def test_switching_group_removes_old_cards(qapp, data):
    """Старые карточки не должны накапливаться в сетке.

    `deleteLater()` отложен: между сменой вкладки и обработкой цикла
    событий старые виджеты ещё живы. Раньше это было видно на экране.

    Проверяем по новому разделу, а не по предыдущему: если бы карточки
    копились, в сетке оказалось бы 4 + 7 = 11.
    """
    from sinaps.ui.panel import Panel

    panel = Panel()
    panel.set_data(data)
    qapp.processEvents()

    panel.show_group("windows")
    qapp.processEvents()
    assert panel._grid.count() == len(data.cards["windows"])   # noqa: SLF001

    panel.show_group("media")
    qapp.processEvents()

    assert panel._grid.count() == len(data.cards["media"]), (  # noqa: SLF001
        "после смены вкладки в сетке остались карточки прошлого раздела"
    )


def test_panel_height_grows_with_cards(qapp, data):
    """Окно подстраивается под содержимое, а не висит пустотой."""
    from sinaps.ui.panel import Panel

    panel = Panel()
    panel.set_data(data)
    panel.show()
    qapp.processEvents()

    panel.show_group("screen")             # 2 карточки
    qapp.processEvents()
    short = panel.height()

    panel.show_group("media")              # 7 карточек
    qapp.processEvents()
    tall = panel.height()

    assert tall > short, "на семи карточках окно должно быть выше"


def test_chips_row_has_buttons(qapp, data):
    """Лента подсказок — часть задачи, а не украшение."""
    from sinaps.ui.panel import Panel

    panel = Panel()
    panel.set_data(data)
    qapp.processEvents()

    assert len(panel._chip_buttons) == len(data.chips)   # noqa: SLF001


def test_chip_click_sends_phrase(qapp, data):
    from sinaps.ui.panel import Panel

    panel = Panel()
    chosen: list[str] = []
    panel.phrase_chosen.connect(chosen.append)
    panel.set_data(data)
    qapp.processEvents()

    panel._chip_buttons[0].click()          # noqa: SLF001
    qapp.processEvents()

    assert chosen and chosen[0] == data.chips[0].safe_phrase


# --- речь -------------------------------------------------------------------


def test_capabilities_speech_promises_the_panel(registry):
    """Речь должна обещать ровно то, что происходит."""
    import asyncio

    from sinaps.core.bus import EventBus, EventType
    from sinaps.skills.engine import SkillEngine
    from sinaps.plugins.testing import FakePlatform

    bus = EventBus()
    engine = SkillEngine(registry=registry, bus=bus, platform=FakePlatform())

    seen: list = []

    async def watcher(event):               # noqa: ANN001
        seen.append(event)

    bus.on(EventType.PANEL_REQUESTED)(watcher)

    async def ask():
        return await engine.respond("что ты умеешь", {"source": "voice"})

    reply = asyncio.run(ask())

    assert reply.show_panel is True
    assert seen, "панель должна запрашиваться событием"


def test_capabilities_speech_does_not_mention_screen(registry):
    """Старая поломка: «Подробности на экране» безо всякого экрана."""
    import asyncio

    from sinaps.skills.engine import SkillEngine
    from sinaps.plugins.testing import FakePlatform

    engine = SkillEngine(registry=registry, platform=FakePlatform())

    async def ask():
        return await engine.respond("что ты умеешь", {"source": "voice"})

    speech = asyncio.run(ask()).speech.lower()
    assert "на экране" not in speech, (
        "речь обещает экран, которого в режиме трея нет"
    )


def test_capabilities_speech_states_a_real_number(registry):
    import asyncio

    from sinaps.skills.engine import SkillEngine
    from sinaps.plugins.testing import FakePlatform

    engine = SkillEngine(registry=registry, platform=FakePlatform())
    data = P.build_cards(registry.specs)

    async def ask():
        return await engine.respond("что ты умеешь", {"source": "voice"})

    speech = asyncio.run(ask()).speech
    assert str(data.total) in speech, (
        f"речь должна назвать настоящее число команд ({data.total})"
    )