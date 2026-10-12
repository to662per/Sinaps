"""Ответ ассистента: что сказать и что показать.

Живёт в `core`, потому что это общий контракт между слоями: навык его
создаёт, конвейер его озвучивает, UI его показывает. Если оставить тип в
`conversation`, навыкам пришлось бы импортировать разговорный слой — и
зависимости перестали бы быть односторонними.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Reply:
    """Результат обработки одной реплики."""

    speech: str                                   # что озвучить
    display: str | None = None                    # что показать на экране
    follow_up: str | None = None                  # подсказка для следующего шага
    #: Интерфейс должен открыть панель возможностей. См. `skills/base.py`.
    show_panel: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.speech

    @property
    def screen_text(self) -> str:
        return self.display or self.speech
