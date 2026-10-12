"""Панель возможностей: что показываем и как.

Модель данных отдельно от виджета
---------------------------------
Всё, что можно проверить без запуска Qt, живёт здесь: сборка карточек
из реестра, подбор примеров, чипы. Виджет в `Panel` только рисует
готовые данные.

Причина не в тестах ради тестов. Панель обещает пользователю, что
ассистент умеет, — и если в списке окажется лишнее, получится ровно
то, ради чего всё затевалось: красивый экран с несуществующими
возможностями. Проверять такое удобнее и надёжнее без окна на экране.

Про вкладки
-----------
Карточки группируются по префиксу намерения, а подписи и порядок берутся
из `skills.taxonomy`. Группа, которой нет в таксономии, не показывается
вовсе: молча спрятать её нельзя (человек не найдёт команду), а показать
под словом «unknown» — значит соврать.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..skills import taxonomy

#: Сколько секунд бездействия закрывает панель.
IDLE_CLOSE_S = 25.0

#: Сколько команд показывать в ленте подсказок.
CHIP_COUNT = 10


@dataclass(frozen=True)
class Card:
    """Одна карточка: что умеет и как это сказать."""

    intent: str              # apps.open
    group: str               # apps
    title: str               # «Открыть программу»
    phrase: str              # «открой блокнот»
    dangerous: bool = False
    icon: str = ""

    @property
    def safe_phrase(self) -> str:
        """Фраза без ведущих и завершающих кавычек."""
        return self.phrase.strip(" \"«»")


@dataclass
class PanelData:
    """Готовое к показу содержимое панели."""

    groups: list[tuple[str, str, str]] = field(default_factory=list)
    # (ключ группы, название со значком, счётчик)
    cards: dict[str, list[Card]] = field(default_factory=dict)
    chips: list[Card] = field(default_factory=list)
    total: int = 0

    def group_titles(self) -> list[str]:
        return [title for _key, title, _count in self.groups]


# ---------------------------------------------------------------------------
# Сборка из реестра
# ---------------------------------------------------------------------------


def _example_phrase(spec: Any) -> str:
    """Пример фразы для карточки.

    Берём `examples` — это то, что человек реально может произнести.
    `patterns` идут следом и часто содержат служебные куски вроде
    `(?P<name>...)`: на карточке такая строка бесполезна.
    """
    for example in (spec.examples or []):
        text = str(example).strip()
        if text:
            return text
    for pattern in (spec.patterns or []):
        text = str(pattern).strip()
        if text and not text.startswith("^") and "?" not in text:
            return text
    return ""


def _card_title(spec: Any, group_title: str) -> str:
    """Человеческое название команды.

    Берём описание из `IntentSpec` — оно заполнено у всех команд и
    именно для человека. Раньше здесь стоял предел в 28 символов, и
    шесть длинных описаний откатывались на название группы: в «Звук и
    музыка» появлялись две карточки с одинаковым заголовком
    «Звук и музыка». Разные заголовки важнее короткой строки — длинное
    описание просто переносится на вторую строку.
    """
    description = (getattr(spec, "description", "") or "").strip()
    return description or group_title


def build_cards(specs: Iterable[Any]) -> PanelData:
    """Собирает панель из намерений реестра.

    Скрытые намерения пропускаются: это внутренние команды и служебные
    слоты, показывать их человеку незачем.
    """
    data = PanelData()
    per_group: dict[str, list[Card]] = {}
    icons: dict[str, str] = {}

    for spec in sorted(specs, key=lambda s: s.name):
        if getattr(spec, "hidden", False):
            continue

        key = taxonomy.group_of(spec.name)
        group = taxonomy.describe(key)
        if group is None:
            # Нет описания — прячем. Показать «unknown» значит показать
            # то, чему нет названия; человек решит, что это ошибка.
            continue

        phrase = _example_phrase(spec)
        if not phrase:
            # Команда без примера фразы бесполезна на карточке: человек
            # не поймёт, что сказать.
            continue

        card = Card(
            intent=spec.name,
            group=key,
            title=_card_title(spec, group.title),
            phrase=phrase,
            dangerous=bool(getattr(spec, "dangerous", False)),
            icon=group.icon,
        )
        per_group.setdefault(key, []).append(card)
        icons[key] = group.icon

    for key in taxonomy.known_groups():
        cards = per_group.get(key)
        if not cards:
            continue
        group = taxonomy.describe(key)
        assert group is not None
        data.cards[key] = cards
        data.groups.append((key, f"{group.icon}  {group.title}", len(cards)))

    data.total = sum(len(cards) for cards in per_group.values())
    data.chips = pick_chips(data)
    return data


# ---------------------------------------------------------------------------
# Чипы
# ---------------------------------------------------------------------------

#: Какие группы хороши для ленты подсказок. Порядок — приоритет:
#: то, что человек нажимает чаще всего, должно быть слева.
_CHIP_GROUPS = ("media", "time", "apps", "windows", "screen", "clipboard")

#: Опасные команды в чипы не попадают: кнопка «выключи компьютер» среди
#: подсказок — отличный способ для глупой шутки и плохой для интерфейса.
#: Подтверждение есть, но зачем просить об этом кнопкой.


def pick_chips(data: PanelData, limit: int = CHIP_COUNT) -> list[Card]:
    """Лента подсказок: безопасные команды из ходовых групп."""
    chips: list[Card] = []
    for key in _CHIP_GROUPS:
        for card in data.cards.get(key, []):
            if card.dangerous:
                continue
            chips.append(card)
            if len(chips) >= limit:
                return chips
    return chips


def chips_for_intent(data: PanelData, intent: str, limit: int = 3) -> list[Card]:
    """Чипы рядом с ответом: что логично сказать дальше.

    Берём соседние команды той же группы. Это дешевле, чем учить ассистента
    связям между командами, и работает предсказуемо: спросил про громкость —
    предложи громче, тише и уровень.
    """
    key = taxonomy.group_of(intent)
    cards = data.cards.get(key, [])
    if len(cards) <= 1:
        return []

    index = next((i for i, c in enumerate(cards) if c.intent == intent), 0)
    picked: list[Card] = []
    # Соседи по кругу: если команда первая, берём следующие.
    for offset in range(1, len(cards)):
        if len(picked) >= limit:
            break
        if offset >= len(cards):
            break
        candidate = cards[(index + offset) % len(cards)]
        if not candidate.dangerous and candidate.intent != intent:
            picked.append(candidate)
    return picked