# -*- coding: utf-8 -*-
"""Подтверждение голосом для действий из интерфейса.

Отзыв прав и выключение плагина — те же опасные действия, что
выключение компьютера, только с меньшими последствиями. Значит и
подтверждаться должны так же: голосом, а не нажатием кнопки.

Честно о границе такого подтверждения. Оно защищает от ошибки и от
того, кто стоит рядом. Оно **не** защищает от плагина, который сам
попросил эти права: если человек один раз одобрил `platform.power`,
дальше плагин может просить его сколько угодно, и каждое подтверждение
пройдёт. Настоящая защита — в другом: код плагина исполняется в
дочернем процессе, где сокеты и `subprocess` не импортируются, а
`platform.power` всё равно спрашивает подтверждение голосом.

Общий механизм, а не две копии в окне: подтверждений будет много — по
одному на плагин, на вид — и каждая копия со временем разошлась бы с
остальными.

Заявка живёт в ядре и переживает перерисовку окна: окно можно закрыть и
открыть заново, а ответ «нет» всё равно должен работать.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

#: Сколько ждём ответа, пока не истекнет.
#:
#: Для команд голосом это 15 секунд — столько же, сколько у опасных
#: команд. Здесь срок по той же причине: человек нажал кнопку и отошёл
#: от компьютера, а через полчаса подтвердил что-то, что забыл.
DEFAULT_TTL_S = 15.0


@dataclass
class GuardRequest:
    """Заявка, ждущая голосового ответа."""

    #: Что именно человек собирается сделать, по-русски. Показывается
    #: и озвучивается: «Отозвать права у weather?» — понятно, «weather»
    #: — нет.
    question: str
    #: Что сделать, когда ответ «да». Принимает описание действия и
    #: возвращает текст результата для озвучки.
    action: Callable[[], Awaitable[str]]
    #: Короткое имя для журнала и для трея.
    label: str
    created_at: float = field(default_factory=time.monotonic)
    ttl: float = DEFAULT_TTL_S

    def is_expired(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        return (moment - self.created_at) > self.ttl


class VoiceGuard:
    """Ждёт голосового подтверждения одного действия за раз.

    Один за раз — намеренно. Два действия, ждущих ответа, означают два
    «да» вслепую: человек подтвердил не то, что думал. Пока занято
    одно, новое действие либо ждёт очереди, либо отказывает.
    """

    def __init__(self, *, ttl: float = DEFAULT_TTL_S) -> None:
        self._pending: GuardRequest | None = None
        self._ttl = ttl

    @property
    def has_pending(self) -> bool:
        """Ждёт ли сейчас ответа.

        Просроченная заявка не считается ожидающей: иначе окошко
        в интерфейсе осталось бы висеть с вопросом, на который ответа
        уже никто не ждёт.
        """
        if self._pending is None:
            return False
        if self._pending.is_expired():
            self._pending = None
            return False
        return True

    @property
    def pending(self) -> GuardRequest | None:
        return self._pending if self.has_pending else None

    @property
    def question(self) -> str | None:
        """Вопрос, если он ждёт ответа."""
        request = self.pending
        return request.question if request is not None else None

    def request(self, question: str, action: Callable[[], Awaitable[str]],
                label: str = "") -> bool:
        """Запросить подтверждение. `False` — уже ждём другого.

        Отказ здесь честнее, чем очередь: человек не помнит, на что
        отвечает, если вопрос менялся.
        """
        if self.has_pending:
            log.info("Голосовое подтверждение занято: %s. Запрос «%s» отклонён",
                     self._pending.label, question)
            return False
        self._pending = GuardRequest(
            question=question, action=action, label=label or question,
            ttl=self._ttl)
        log.info("Жду подтверждения голосом: %s", self._pending.label)
        return True

    async def answer(self, text: str, *, is_yes, is_no
                     ) -> tuple[bool, str]:
        """Обработать ответ. `(выполнено, текст для человека)`.

        `is_yes` и `is_no` — те же распознаватели, что у команд: здесь
        не нужен второй набор правил, иначе «да» означало бы разное в
        разных местах.
        """
        request = self._pending
        if request is None:
            return False, "Нечего подтверждать"

        if request.is_expired():
            self._pending = None
            log.info("Подтверждение «%s» истекло", request.label)
            return False, "Время вышло, отменил"

        if is_no(text):
            self._pending = None
            log.info("Отклонено голосом: %s", request.label)
            return False, "Хорошо, отменяю"

        if not is_yes(text):
            self._pending = None
            log.info("Неясный ответ на «%s», отменено", request.label)
            return False, "Не понял, подтверждаешь или нет"

        self._pending = None
        try:
            result = await request.action()
        except Exception:                         # noqa: BLE001
            # Подтверждение получено, а действие не вышло. Молчать здесь
            # нельзя: человек решил бы, что всё сделано.
            log.exception("Подтверждённое действие «%s» не выполнилось",
                          request.label)
            return False, "Не получилось выполнить, подробности в журнале"

        return True, result or "Готово"

    def cancel(self) -> None:
        """Снять ожидание. Вызывается при остановке ассистента."""
        if self._pending is not None:
            log.info("Ожидание подтверждения снято: %s",
                     self._pending.label)
            self._pending = None


__all__ = ["DEFAULT_TTL_S", "GuardRequest", "VoiceGuard"]