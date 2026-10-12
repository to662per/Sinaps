"""Контекст диалога: всё, что нужно навыку для работы.

Навык не должен знать, откуда пришла реплика (голос или текст), где мы
находимся в диалоге и как устроена память. Вместо этого он получает один
объект `DialogContext` и работает только с ним.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..platform.base import PlatformCapabilities
    from ..skills.base import Response


@dataclass
class PendingAction:
    """Действие, ждущее подтверждения пользователя."""

    intent: str
    slots: dict[str, Any]
    question: str
    created_at: float = 0.0
    ttl: float = 15.0

    def is_expired(self, now: float) -> bool:
        return (now - self.created_at) > self.ttl


@dataclass
class DialogContext:
    """Состояние одного диалога."""

    text: str
    source: str = "voice"
    confidence: float = 1.0
    history: list[dict[str, str]] = field(default_factory=list)

    platform: "PlatformCapabilities | None" = None
    session: Any = None
    user_name: str = "Капитан"

    #: Намерение, для которого мы уже спросили уточнение.
    pending_intent: str | None = None
    #: Действие, ждущее подтверждения.
    pending_action: PendingAction | None = None

    # --- удобные ярлыки ---------------------------------------------------

    @property
    def from_voice(self) -> bool:
        return self.source == "voice"

    @property
    def needs_confirmation(self) -> bool:
        return self.pending_action is not None

    def remember(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        del self.history[:-12]

    def last_user_text(self) -> str:
        for entry in reversed(self.history):
            if entry["role"] == "user":
                return entry["content"]
        return ""

    def is_yes(self, text: str) -> bool:
        """Понимает согласие. Русские «да», «ага», «ок», «угу» и т.п."""
        answer = (text or "").strip().lower()
        return answer.startswith((
            "да", "ага", "ок", "окей", "угу", "хорошо", "подтверждаю",
            "согласен", "согласна", "точно", "давай",
        ))

    def is_no(self, text: str) -> bool:
        answer = (text or "").strip().lower()
        return answer.startswith((
            "нет", "неа", "не", "отмена", "отменить", "стоп", "хватит",
            "не надо", "не нужно",
        ))

    def reply(self, speech: str, **kwargs) -> "Response":
        """Удобное создание ответа — чтобы навык не импортировал Response."""
        from ..skills.base import Response

        return Response(speech=speech, **kwargs)


class ContextFactory:
    """Создаёт контексты. Нужен, чтобы не плодить конструкторы вручную."""

    def __init__(self, platform=None, session=None, user_name: str = "Капитан") -> None:
        self.platform = platform
        self.session = session
        self.user_name = user_name

    def build(self, text: str, **kwargs) -> DialogContext:
        return DialogContext(
            text=text,
            platform=self.platform,
            session=self.session,
            user_name=self.user_name,
            **kwargs,
        )
