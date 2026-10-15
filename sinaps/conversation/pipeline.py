"""Конвейер разговора: реплика -> намерение -> навык -> ответ.

На этапе 1-2 намерения ещё нет, поэтому работает `EchoResponder`: он
подтверждает, что услышал. Начиная с этапа 2 подключается настоящий
`IntentResolver` и набор навыков — но конвейер не меняется, меняется только
то, что стоит в `_responder`.

Конвейер отвечает за:
  * переходы FSM;
  * замеры задержек по стадиям (vad_end -> stt_done -> intent -> skill -> first_audio);
  * подтверждение опасных действий;
  * корректное прерывание ответа пользователем (barge-in);
  * гарантию, что на один ввод приходится ровно один ответ.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from ..core.bus import EventBus, EventType
from ..core.context import DialogContext
from ..core.errors import DangerousActionDenied, SinapsError
from ..core.guard import VoiceGuard
from ..core.logging_setup import (log_assistant, log_dialogue_error,
                                  log_dialogue_guard, log_user)
from ..core.metrics import Metrics, TurnMetrics
from ..core.response import Reply
from ..core.session import SessionController, State
from ..tts.base import TTSEngine

log = logging.getLogger(__name__)

#: Тип функции, превращающей реплику в ответ. Меняется по мере роста проекта.
Responder = Callable[[str, dict], Awaitable[Reply]]


class ConversationPipeline:
    def __init__(
        self,
        *,
        bus: EventBus,
        session: SessionController,
        tts: TTSEngine,
        player,
        metrics: Metrics,
        responder: Responder,
        speak_enabled: bool = True,
        confirm_timeout_s: float = 10.0,
        memory: "MemoryStore | None" = None,
        guard: "VoiceGuard | None" = None,
    ) -> None:
        self.bus = bus
        self.session = session
        self.tts = tts
        self.player = player
        self.metrics = metrics
        self.responder = responder
        self.speak_enabled = speak_enabled
        self.confirm_timeout_s = confirm_timeout_s
        # Хранилище истории. Необязательно: без него (тесты, `--text-only`
        # без БД) разговор просто не пишется на диск, но не ломается.
        self.memory = memory
        #: Подтверждение голосом для действий интерфейса. Необязателен
        #: по той же причине: без него ассистент просто не задаёт таких
        #: вопросов, и разговор работает как раньше.
        self.guard = guard

        self._speaking = False
        self._history: list[dict[str, str]] = []

    # --- подписка на события -----------------------------------------------

    def install(self) -> None:
        self.bus.on(EventType.TRANSCRIPT_FINAL)(self._on_transcript)
        self.bus.on(EventType.SPEECH_START)(self._on_speech_start)

    @property
    def history(self) -> list[dict[str, str]]:
        return list(self._history)

    def handle_text_input(self, text: str) -> asyncio.Task:
        """Реплика из текстовой консоли — тот же путь, что и голосовая."""
        text = (text or "").strip()
        if not text:
            return asyncio.create_task(asyncio.sleep(0))
        return asyncio.create_task(self._process(text, source="text"))
    def _remember(self, user_text: str, reply: Reply, source: str, *,
                  started: float) -> None:
        """Сохранить обмен в SQLite.

        Пишем и намерение, и задержку: по истории человек должен видеть не
        только «что было сказано», но и «сколько ждал». Обмен с нулевой
        задержкой выглядит как мгновенный ответ — и это врёт.

        Ошибки глушим целиком: потерянная запись в истории не должна
        превращаться в молчание ассистента.
        """
        if self.memory is None:
            return
        try:
            meta = reply.meta or {}
            intent = str(meta.get("intent", ""))
            latency_ms = int((time.monotonic() - started) * 1000)

            self.memory.add_turn(user_text, reply.speech, intent=intent,
                                 source=source, total_ms=latency_ms)
            self.memory.add_dialogue("user", user_text, source=source)
            self.memory.add_dialogue(
                "assistant", reply.speech, source=source,
                intent=intent, latency_ms=latency_ms,
                meta={k: v for k, v in meta.items() if k != "text"},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("История: не удалось сохранить обмен (%s)", exc)

    # --- обработка ---------------------------------------------------------

    async def _on_speech_start(self, event) -> None:  # noqa: ANN001
        # Пользователь заговорил, пока ассистент говорил -> замолкаем.
        if self._speaking:
            log.debug("Barge-in: прерываю ответ")
            self.player.cancel()
            await self.bus.emit(EventType.BARGE_IN)

    async def _on_transcript(self, event) -> None:  # noqa: ANN001
        text = (event.payload.get("text") or "").strip()
        if not text:
            return
        turn: TurnMetrics | None = event.payload.get("metrics")
        await self._process(text, source="voice", turn=turn,
                            confidence=event.payload.get("confidence", 1.0))

    async def _process(
        self,
        text: str,
        *,
        source: str,
        turn: TurnMetrics | None = None,
        confidence: float = 1.0,
    ) -> None:
        if not await self.session.try_acquire():
            # Молча проглатывать нельзя: пользователь увидит «обрабатываю…»
            # и решит, что ассистент завис. Лучше честно сказать, что занято.
            log.debug("Диалог занят, реплика '%s' отброшена", text[:40])
            await self.bus.emit(EventType.RESPONSE_READY,
                                text="Секунду, я ещё отвечаю на прошлую команду",
                                display="сессия занята, реплика отброшена",
                                source=source)
            return

        turn = turn or self.metrics.new_turn()
        turn.mark("stt_done")
        # Момент начала обработки — для задержки в истории. Отсчёт берём
        # до всех проверок: иначе «0 мс» в истории означало бы мгновенный
        # ответ, а на деле это четыре с лишним секунды ожидания.
        started = time.monotonic()
        # Для реплики из консоли стадии VAD не было, но для метрики ответа
        # нужна точка отсчёта: считаем её от момента получения текста.
        # Иначе `response` всегда 0 для текстового ввода.
        if turn.marks.get("vad_end") is None:
            turn.mark("vad_end")
        log_user(text)

        try:
            # --- подтверждение голосом для действий интерфейса ---
            #
            # Проверяется **до** разбора команды. Иначе «да» превратилось
            # бы в «не знаю такую команду»: движок не знает ни слова про
            # отзыв прав, и человек услышал бы отказ вместо ответа.
            guard_reply = await self._answer_guard(text, source=source)
            if guard_reply is not None:
                # THINKING обязателен и здесь: `_say` переводит сессию
                # в SPEAKING, а переход `idle -> speaking` запрещён
                # таблицей. Без этой строки озвучка ответа падала бы
                # целиком, и голосовое подтверждение не работало бы.
                await self.session.transition(State.THINKING, reason="guard")
                turn.mark("intent")
                self._history.append({"role": "user", "content": text})
                self._history.append(
                    {"role": "assistant", "content": guard_reply.speech})
                del self._history[:-20]
                self._remember(text, guard_reply, source, started=started)
                await self._say(guard_reply, turn=turn, source=source)
                return

            # --- понимание ---
            await self.session.transition(State.THINKING, reason="understand")
            reply = await self.responder(text, {
                "source": source,
                "confidence": confidence,
                "history": list(self._history[-6:]),
            })
            turn.mark("intent")

            if not reply or not reply.speech.strip():
                reply = Reply(speech="Я не знаю, что на это ответить.")

            # --- ответ ---
            self._history.append({"role": "user", "content": text})
            self._history.append({"role": "assistant", "content": reply.speech})
            del self._history[:-20]
            self._remember(text, reply, source, started=started)

            await self._say(reply, turn=turn, source=source)

        except DangerousActionDenied:
            log_assistant("Отменено")
            await self._say(Reply(speech="Хорошо, отменяю"), turn=turn, source=source)
        except SinapsError as exc:
            log_dialogue_error(exc.message)
            await self._say(Reply(speech=exc.speech), turn=turn, source=source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Необработанная ошибка в диалоге")
            await self._say(
                Reply(speech="Что-то пошло не так, попробуй ещё раз"),
                turn=turn,
                source=source,
            )
            await self.bus.emit(EventType.ERROR, where="conversation", detail=str(exc))
        finally:
            self.metrics.finish(turn)
            # `reset`, а не `transition(IDLE)`: после любой фазы диалога
            # сессия обязана закрыться, а таблица переходов допускает IDLE
            # не из каждого состояния (например, не из THINKING, если
            # озвучка выключена и SPEAKING не случился).
            await self.session.reset(reason="dialogue_done")
            self.session.release()

    async def _answer_guard(self, text: str, *, source: str) -> Reply | None:
        """Ответ на вопрос подтверждения голосом.

        Возвращает `None`, если вопроса нет и реплику надо понимать
        обычным порядком. Проверка стоит до разбора: иначе «да» на
        вопрос про отзыв прав дошло бы до движка как неизвестная команда.
        """
        guard = self.guard
        if guard is None or not guard.has_pending:
            return None

        probe = DialogContext(text=text, source=source)
        done, message = await guard.answer(
            text, is_yes=probe.is_yes, is_no=probe.is_no)

        log_dialogue_guard(message)
        return Reply(speech=message, display=message)

    async def _say(self, reply: Reply, *, turn: TurnMetrics | None, source: str) -> None:
        text = reply.speech.strip()
        if not text:
            return

        log_assistant(text)
        await self.bus.emit(EventType.RESPONSE_READY, text=text,
                            display=reply.display or text, source=source)

        if not self.speak_enabled:
            turn and turn.mark("first_audio")
            return

        self._speaking = True
        await self.session.transition(State.SPEAKING, reason="reply")
        await self.bus.emit(EventType.TTS_STARTED, text=text)
        try:
            played = await self.tts.speak(text, self.player)
            # Отметка «первый звук» — даже если звука не было, это честная
            # точка окончания обработки: метрика ответа измеряется до звука.
            turn and turn.mark("first_audio")
            if not played:
                log.debug("Ответ не озвучен, напечатан текстом: %s", text[:60])
        except asyncio.CancelledError:
            turn and turn.mark("first_audio")
            raise
        except Exception as exc:
            log.error("Не удалось озвучить ответ: %s", exc)
        finally:
            self._speaking = False
            await self.bus.emit(EventType.TTS_FINISHED, text=text)

    # --- управление -------------------------------------------------------

    async def stop_speaking(self) -> None:
        """Замолчать немедленно (кнопка «стоп» в трее)."""
        self.player.cancel()
        self._speaking = False

    def set_speak_enabled(self, enabled: bool) -> None:
        self.speak_enabled = enabled

    @property
    def is_speaking(self) -> bool:
        return self._speaking


def make_echo_responder(*, assistant_name: str = "Синапс") -> Responder:
    """Ответчик этапа 1: подтверждает, что услышал пользователя.

    Существует не для красоты — он доказывает, что замкнут весь контур
    (микрофон -> VAD -> STT -> конвейер -> TTS -> динамики) и замеряет
    реальную задержку до появления первого звука.
    """

    async def responder(text: str, context: dict) -> Reply:
        source = context.get("source", "?")
        return Reply(
            speech=f"Услышал: {text}",
            display=f"[{source}] {text}",
        )

    responder.__name__ = "echo_responder"  # type: ignore[attr-defined]
    return responder
