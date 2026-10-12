"""Движок понимания: текст -> намерение -> действие -> ответ.

Это «мозг» ассистента на этапе 2. Он заменяет заглушку `echo_responder`
и решает четыре задачи:

1. Сопоставить реплику с командой через реестр навыков.
2. Ответить на «да/нет» для опасного действия (подтверждение).
3. Принять уточняющий ответ, если слот не распознан.
4. Если команда не найдена — честно сказать об этом и показать справку.

Ключевое правило: **не выдумывать**. Если слот не распознан, ассистент
спрашивает, а не угадывает.
"""

from __future__ import annotations

import logging
import time

from ..core.bus import EventBus, EventType
from ..core.context import ContextFactory, DialogContext, PendingAction
from ..core.errors import DangerousActionDenied, SinapsError
from ..core.response import Reply
from ..nlu.normalize import normalize
from .base import Response
from .registry import Match, SkillRegistry

log = logging.getLogger(__name__)

#: Что говорить, когда команда не найдена. Разные формулировки для голоса и
#: текста: голосом длинное предложение раздражает.
_NOT_FOUND_SHORT = "Не понял"
_NOT_FOUND_VOICE = "Не понял команду. Скажи «что ты умеешь»"
_NOT_FOUND_TEXT = "Не понял команду. Наберите /help, чтобы увидеть список."


class SkillEngine:
    """Превращает реплику пользователя в действие."""

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        platform=None,
        session=None,
        bus: EventBus | None = None,
        user_name: str = "Капитан",
        confirm_ttl: float = 15.0,
    ) -> None:
        self.registry = registry
        self.bus = bus
        self.session = session
        self.user_name = user_name
        self.confirm_ttl = confirm_ttl

        self._factory = ContextFactory(
            platform=platform, session=session, user_name=user_name
        )
        self._pending: PendingAction | None = None
        self._pending_slot: tuple[str, str] | None = None   # (intent, slot)

    # --- публичный вход ---------------------------------------------------

    async def respond(self, text: str, context: dict) -> Reply:
        """Основная точка входа. Должна никогда не бросать исключения."""
        ctx = self._factory.build(
            text,
            source=context.get("source", "voice"),
            confidence=context.get("confidence", 1.0),
            history=list(context.get("history") or []),
        )

        try:
            reply = await self._handle(ctx)
        except DangerousActionDenied:
            reply = Reply(speech="Хорошо, отменяю")
        except SinapsError as exc:
            reply = Reply(speech=exc.speech)
        except Exception:
            log.exception("Сбой в движке понимания")
            reply = Reply(speech="Что-то сломалось, попробуй ещё раз")

        if reply.speech:
            ctx.remember("assistant", reply.speech)
        return reply

    # --- внутренняя логика -------------------------------------------------

    async def _handle(self, ctx: DialogContext) -> Reply:
        text = ctx.text.strip()
        if not text:
            return Reply(speech="Я не расслышал")

        # 1. Ответ на уточняющий вопрос.
        if self._pending_slot:
            return await self._handle_slot_answer(ctx)

        # 2. Ответ на подтверждение опасного действия.
        if self._pending is not None:
            return await self._handle_confirmation(ctx)

        # 3. Обычный разбор команды.
        return await self._handle_command(ctx)

    async def _handle_command(self, ctx: DialogContext) -> Reply:
        normalized = normalize(ctx.text)
        # Оригинал передаём отдельно: из него берутся имена файлов,
        # где «ё» ещё не заменена.
        match = self.registry.match(normalized, ctx.text)

        if match is None:
            return self._not_found(ctx)

        await self._announce(match.spec.name)

        # Опасное действие — спрашиваем подтверждение.
        if match.spec.dangerous:
            return await self._request_confirmation(ctx, match)

        return await self._execute(ctx, match)

    async def _execute(self, ctx: DialogContext, match: Match) -> Reply:
        response = await self.registry.execute(match, ctx)
        return Reply(
            speech=response.speech,
            display=response.display or response.speech,
            follow_up=response.follow_up,
            # Намерение и слоты едут в ответе: без них в истории разговоров
            # остаётся голый текст, и разобраться, почему фраза не сработала,
            # невозможно — а это ровно то, ради чего история нужна.
            meta={
                "intent": match.spec.name,
                "slots": dict(match.slots),
                "score": round(float(match.score), 3),
                "text": match.matched_text,
            },
        )

    # --- подтверждение -----------------------------------------------------

    async def _request_confirmation(self, ctx: DialogContext, match: Match) -> Reply:
        """Запрашивает подтверждение и запоминает отложенное действие.

        Сразу ничего не выполняем: пользователь должен ответить «да» отдельной
        репликой. Это защищает от «синапс выключи компьютер», произнесённого
        в шумной комнате.
        """
        self._pending = PendingAction(
            intent=match.spec.name,
            slots=match.slots,
            question=match.spec.description,
            created_at=time.time(),
            ttl=self.confirm_ttl,
        )
        await self._announce_pending(match.spec.name, "confirm")

        question = match.spec.description or "Выполнить действие?"
        return Reply(
            speech=f"{question}. Подтверди: да или нет.",
            display=f"ожидаю подтверждения: {match.spec.name}",
        )

    async def _handle_confirmation(self, ctx: DialogContext) -> Reply:
        pending = self._pending
        assert pending is not None

        if pending.is_expired(time.time()):
            self._pending = None
            await self._announce_pending(pending.intent, "expired")
            return Reply(
                speech="Время подтверждения вышло, отменил",
                display="подтверждение истекло",
            )

        if ctx.is_no(ctx.text):
            self._pending = None
            await self._announce_pending(pending.intent, "denied")
            return Reply(speech="Отменил", display="отменено пользователем")

        if not ctx.is_yes(ctx.text):
            self._pending = None
            await self._announce_pending(pending.intent, "unclear")
            return Reply(
                speech="Не понял, подтверждаешь или нет",
                display="ожидалось «да» или «нет»",
            )

        # Подтверждено — выполняем отложенное действие.
        self._pending = None
        spec = self.registry.find_spec(pending.intent)
        match = Match(spec=spec, raw_slots={}, slots=pending.slots)
        await self._announce_pending(pending.intent, "confirmed")
        return await self._execute(ctx, match)

    # --- уточняющий ответ -------------------------------------------------

    async def _handle_slot_answer(self, ctx: DialogContext) -> Reply:
        """Принимает ответ на вопрос «что именно?»."""
        assert self._pending_slot is not None
        intent_name, slot_name = self._pending_slot
        self._pending_slot = None

        spec = self.registry.find_spec(intent_name)
        text = normalize(ctx.text)

        # Пробуем разобрать ответ как значение слота.
        slot = spec.slots.get(slot_name)
        if slot is not None:
            try:
                value = self.registry._parse_value(slot, text)  # noqa: SLF001
                slots = {slot_name: value}
                # Обязательные слоты, которые уже были разобраны, не теряем.
                match = self.registry.match(text) or Match(spec=spec, raw_slots={}, slots={})
                merged = dict(match.slots)
                merged.update(slots)
                return await self._execute(ctx, Match(spec=spec, raw_slots={}, slots=merged))
            except SinapsError:
                pass

        # Ответ не помог — возвращаемся к общему разбору.
        return await self._handle_command(ctx)

    # --- вспомогательное --------------------------------------------------

    def _not_found(self, ctx: DialogContext) -> Reply:
        message = _NOT_FOUND_VOICE if ctx.from_voice else _NOT_FOUND_TEXT
        total = len(self.registry)
        return Reply(
            speech=message,
            display=f"известно команд: {total}",
            follow_up="Скажи «что ты умеешь», и я покажу список.",
        )

    async def _announce(self, intent: str) -> None:
        if self.bus:
            await self.bus.emit(EventType.INTENT_RESOLVED, intent=intent)

    async def _announce_pending(self, intent: str, status: str) -> None:
        if self.bus:
            await self.bus.emit(EventType.CONFIRM_REQUESTED,
                                intent=intent, status=status)

    # --- состояние --------------------------------------------------------

    @property
    def has_pending(self) -> bool:
        return self._pending is not None or self._pending_slot is not None

    def reset_pending(self) -> None:
        self._pending = None
        self._pending_slot = None


def build_default_registry() -> SkillRegistry:
    """Собирает реестр со всеми стандартными навыками."""
    from . import apps, media, power, screen, smalltalk, system_info, time_date

    registry = SkillRegistry()

    # Порядок регистрации определяет порядок в справке.
    registry.add(time_date.register())
    registry.add(smalltalk.register(registry))
    registry.add(apps.register())
    registry.add(media.register())
    registry.add(system_info.register())
    registry.add(screen.register())
    registry.add(screen.register_clipboard())
    registry.add(screen.register_windows())
    registry.add(power.register())

    return registry


__all__ = ["SkillEngine", "build_default_registry"]
