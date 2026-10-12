"""Декларация навыка — единственный источник истины о команде.

Ключевая идея всего проекта: команда описывается ОДИН РАЗ в `IntentSpec`.
Из этого описания автоматически строятся:
  * регулярные выражения для быстрого пути (`patterns`);
  * слоты и их типы (`slots`);
  * признак опасности (`dangerous`) — от него зависит подтверждение;
  * справка «что ты умеешь».

Благодаря этому регулярный путь и LLM не могут разойтись в том, какие
команды вообще существуют: оба строятся из одного списка.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import DialogContext

SlotType = Literal["string", "number", "duration", "time", "datetime", "bool", "path", "freeform"]


@dataclass
class SlotSpec:
    """Описание одного слота — куска команды, который нужно извлечь."""

    name: str
    type: SlotType = "string"
    required: bool = True
    default: Any = None
    description: str = ""
    #: Минимальная длина для строковых слотов («привет» вместо «а»).
    min_length: int = 1

    def validate(self, value: Any) -> Any:
        """Проверяет и приводит значение. Бросает `InvalidSlot`."""
        from ..core.errors import InvalidSlot

        if value is None or (isinstance(value, str) and not value.strip()):
            if self.required and self.default is None:
                raise InvalidSlot(self.name, value, "пустое значение")
            return self.default

        if isinstance(value, str) and len(value.strip()) < self.min_length:
            raise InvalidSlot(
                self.name, value,
                f"слишком коротко, нужно от {self.min_length} символов",
            )
        return value


@dataclass
class Response:
    """Что навык возвращает конвейеру."""

    speech: str                                   # что озвучить
    display: str | None = None                    # что показать в UI
    follow_up: str | None = None                  # «ещё что-нибудь?»
    #: Если задано — конвейер задаст уточняющий вопрос и ждёт ответа.
    ask_for: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.speech


@dataclass
class IntentSpec:
    """Полное описание одной команды."""

    name: str
    handler: Callable[..., Awaitable[Response]]
    patterns: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    slots: dict[str, SlotSpec] = field(default_factory=dict)
    dangerous: bool = False
    description: str = ""
    #: Приоритет: выше — проверяется раньше. Нужен для пересекающихся
    #: шаблонов, например «пауза» против «поставь паузу».
    priority: int = 0
    #: Навык не показывается в справке (внутренние команды).
    hidden: bool = False

    _compiled: list[re.Pattern] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.compile()

    def compile(self) -> None:
        """Компилирует шаблоны.

        Каждая буква «ё» в шаблоне разворачивается в класс `[её]`.
        Причина: текст нормализуется с заменой «ё» -> «е», поэтому шаблон
        «сверни всё» никогда не совпал бы с нормализованным «сверни все».
        Писать «все» вручную в каждом шаблоне — источник тихих ошибок.
        """
        self._compiled = [
            re.compile(pattern.replace("ё", "[её]"), re.IGNORECASE | re.UNICODE)
            for pattern in self.patterns
        ]

    def match(self, text: str) -> dict[str, str] | None:
        """Ищет первый подходящий шаблон. Возвращает именованные группы."""
        for pattern in self._compiled:
            found = pattern.search(text)
            if found:
                return found.groupdict()
        return None

    def to_tool_schema(self) -> dict[str, Any]:
        """Описание инструмента для LLM. Из той же декларации."""
        return {
            "type": "function",
            "function": {
                "name": self.name.replace(".", "_"),
                "description": self.description or f"Команда {self.name}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        slot.name: {
                            "type": "string",
                            "description": slot.description or f"Слот {slot.name} ({slot.type})",
                        }
                        for slot in self.slots.values()
                    },
                    "required": [s.name for s in self.slots.values() if s.required],
                },
            },
        }


class Skill:
    """Навык — набор команд одной темы («приложения», «звук», «питание»)."""

    def __init__(self, name: str, *, description: str = "") -> None:
        self.name = name
        self.description = description
        self.intents: list[IntentSpec] = []

    def command(
        self,
        name: str,
        handler: Callable[..., Awaitable[Response]],
        *,
        patterns: list[str] | None = None,
        examples: list[str] | None = None,
        slots: dict[str, SlotSpec] | None = None,
        dangerous: bool = False,
        description: str = "",
        priority: int = 0,
        hidden: bool = False,
    ) -> IntentSpec:
        """Регистрирует команду. Возвращает спецификацию для доработки."""
        spec = IntentSpec(
            name=f"{self.name}.{name}",
            handler=handler,
            patterns=patterns or [],
            examples=examples or [],
            slots=slots or {},
            dangerous=dangerous,
            description=description or self.description,
            priority=priority,
            hidden=hidden,
        )
        self.intents.append(spec)
        return spec

    def __repr__(self) -> str:
        return f"Skill({self.name}, команд: {len(self.intents)})"
