"""Пайплайн распознавания: микрофон -> VAD -> STT -> событие TRANSCRIPT_FINAL.

Класс `ListeningPipeline` — единственное место, где описан порядок стадий.
Он ничего не «понимает» (это делает nlu) и ничего не «озвучивает» (это делает
tts) — только превращает поток звука в текстовые события.

Параллельно с этим он следит за состоянием сессии и умеет:
  * активироваться по хоткею или внешнему событию (`activate()`);
  * отменять текущую запись;
  * ловить barge-in во время ответа ассистента.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..audio.capture import SAMPLE_RATE, AudioCapture, write_wav
from ..core.bus import EventBus, EventType
from ..core.config import PROJECT_ROOT
from ..core.metrics import Metrics, TurnMetrics
from ..core.session import InvalidTransition, SessionController, State
from ..stt.base import STTEngine, Transcript
from ..vad.silero_vad import SpeechSegmenter, build_vad

log = logging.getLogger(__name__)


@dataclass
class PipelineStats:
    utterances: int = 0
    empty: int = 0
    errors: int = 0
    total_stt_time: float = 0.0

    @property
    def avg_stt_time(self) -> float:
        return self.total_stt_time / self.utterances if self.utterances else 0.0


class ListeningPipeline:
    """Ведёт микрофон и публикует распознанные реплики.

    Работает в фоне постоянно. «Слушает» он всегда (нужно для VAD), но
    обрабатывает фразу только когда сессия находится в состоянии LISTENING.
    """

    def __init__(
        self,
        *,
        capture: AudioCapture,
        stt: STTEngine,
        vad_engine: str = "silero",
        session: SessionController,
        bus: EventBus,
        metrics: Metrics,
        vad_threshold: float = 0.5,
        silence_ms: int = 600,
        min_speech_ms: int = 300,
        max_utterance_s: float = 15.0,
        listen_timeout_s: float = 8.0,
        require_activation: bool = True,
        min_confidence: float = 0.25,
        stuck_timeout_s: float = 30.0,
        record_audio: bool = False,
        record_dir: Path | None = None,
    ) -> None:
        self.capture = capture
        self.stt = stt
        self.session = session
        self.bus = bus
        self.metrics = metrics
        self.require_activation = require_activation
        self.listen_timeout_s = listen_timeout_s
        self.min_confidence = min_confidence
        self.stuck_timeout_s = stuck_timeout_s
        self.record_audio = record_audio
        self.record_dir = record_dir or (PROJECT_ROOT / "data" / "audio")
        self.stats = PipelineStats()

        self.segmenter = SpeechSegmenter(
            build_vad(vad_engine, threshold=vad_threshold),
            sample_rate=capture.sample_rate,
            frame_ms=capture.frame_ms,
            threshold=vad_threshold,
            silence_ms=silence_ms,
            min_speech_ms=min_speech_ms,
            max_utterance_s=max_utterance_s,
        )

        self._task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._listen_until: float | None = None  # deadline ожидания речи
        self._activated = not require_activation
        self._paused = False
        # Предупреждение о немом микрофоне уже прозвучало?
        self._quiet_warned = False
        self._wake_event = asyncio.Event()
        self._turn: TurnMetrics | None = None
        # Аудио, накопленное ДО начала речи. Подклеивается к фразе, чтобы
        # не потерять первое слово — иначе «Сделай громче» превращается
        # в «Громче». Буфер всегда заполнен, поэтому берём его в момент
        # обнаружения речи, а не в момент активации.
        self._pending_preroll: np.ndarray | None = None

    # --- запуск ------------------------------------------------------------

    async def start(self) -> None:
        await self.capture.start()
        self._task = asyncio.create_task(self._run(), name="sinaps-listening")
        if self.stuck_timeout_s > 0:
            self._watchdog_task = asyncio.create_task(
                self._watchdog(), name="sinaps-watchdog")
        log.info("Контур прослушивания запущен")

    async def stop(self) -> None:
        for task in (self._task, self._watchdog_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._watchdog_task = None
        await self.capture.stop()
        log.info("Контур прослушивания остановлен")

    # --- активация ---------------------------------------------------------

    async def activate(self, *, source: str = "manual", timeout_s: float | None = None) -> bool:
        """Открыть окно прослушивания. False — если диалог уже идёт."""
        if self._paused:
            log.debug("Активация отклонена: пауза")
            return False
        if self.session.busy:
            log.debug("Активация проигнорирована: сессия занята (%s)", self.session.state.value)
            return False

        await self.session.transition(State.LISTENING, reason=source)
        self._activated = True
        self._listen_until = time.monotonic() + (
            timeout_s if timeout_s is not None else self.listen_timeout_s
        )
        self.segmenter.reset()
        self._turn = self.metrics.new_turn()

        # Начало окна = момент отсчёта задержки.
        self._turn.mark("vad_start")
        await self.bus.emit(EventType.ACTIVATED, source=source)
        return True

    async def set_paused(self, paused: bool) -> None:
        """Пауза из трея: перестаём принимать голос, но не выходим.

        Микрофон при этом остаётся открыт — закрывать и открывать поток
        дорого, а на паузе всё равно никто не говорит.

        Событие `DEACTIVATED` присылает сам `deactivate`: дублировать его
        здесь нельзя, иначе подписчики (в том числе индикатор трея)
        увидят отключение дважды.
        """
        if self._paused == paused:
            return
        self._paused = paused
        if paused:
            await self.deactivate(reason="paused")
        log.info("Прослушивание %s",
                 "приостановлено" if paused else "возобновлено")

    async def deactivate(self, *, reason: str = "cancel") -> None:
        """Закрыть окно прослушивания, не дожидаясь фразы."""
        self._activated = not self.require_activation
        self._listen_until = None
        self._pending_preroll = None
        self.segmenter.reset()
        if self.session.state is State.LISTENING:
            await self.session.transition(State.IDLE, reason=reason)
        await self.bus.emit(EventType.DEACTIVATED, reason=reason)

    # --- основной цикл -----------------------------------------------------

    async def _run(self) -> None:
        """Основной цикл. Переживает единичные сбои в отдельной фразе.

        Раньше любое исключение из `_on_frame` завершало цикл целиком:
        контур прослушивания умирал, микрофон формально продолжал
        работать, а голос не принимался — до перезапуска программы.
        Ошибка в одной фразе не должна убивать прослушивание.
        """
        try:
            async for frame in self.capture.stream():
                try:
                    await self._on_frame(frame)
                except asyncio.CancelledError:
                    raise
                except InvalidTransition as exc:
                    # Наиболее частый случай: фраза пришла не в LISTENING
                    # (микрофон услышал голос ассистента). Это не поломка,
                    # а нормальная ситуация — просто отбрасываем фразу.
                    log.info("Фраза отброшена: %s", exc)
                    self._recover_after_bad_frame()
                except Exception:
                    log.exception("Сбой при обработке кадра — продолжаю")
                    self._recover_after_bad_frame()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Контур прослушивания упал")
            await self.bus.emit(EventType.ERROR, where="listening")

    def _recover_after_bad_frame(self) -> None:
        """Вернуть конвейер в согласованное состояние после сбоя.

        Сегментер мог остаться внутри фразы, а сессия — в состоянии,
        из которого продолжить нельзя. Без этого следующий же кадр
        уронил бы цикл снова.
        """
        self._pending_preroll = None
        self._turn = None
        try:
            self.segmenter.reset()
        except Exception:
            log.debug("Не удалось сбросить сегментер", exc_info=True)
        if self.session.busy:
            try:
                self.session.reset_state()
            except Exception:
                log.debug("Не удалось сбросить сессию", exc_info=True)

    async def _on_frame(self, frame: np.ndarray) -> None:
        # 1. Проверяем, не истекло ли окно прослушивания.
        if self._activated and self._listen_until is not None:
            if time.monotonic() > self._listen_until:
                log.info("Окно прослушивания истекло — жду активации")
                self._activated = not self.require_activation
                self._listen_until = None
                if self.session.state is State.LISTENING:
                    await self.session.transition(State.IDLE, reason="listen_timeout")

        # 2. Пропускаем кадры, если окно закрыто. VAD всё равно считает —
        #    это позволяет отслеживать barge-in во время ответа.
        if not self._activated and self.require_activation:
            return

        was_in_speech = self.segmenter.in_speech
        utterance = self.segmenter.feed(frame)
        now_in_speech = self.segmenter.in_speech

        # 3. Начало речи — событие для UI («Слушаю...») и точка отсчёта.
        if now_in_speech and not was_in_speech:
            self._pending_preroll = self._take_preroll()
            if self._turn is None:
                self._turn = self.metrics.new_turn()
            await self.bus.emit(EventType.SPEECH_START)

        # 4. Фраза закончилась — распознаём.
        if utterance is not None:
            # Главная защита от падения. Микрофон слышит голос ассистента
            # во время ответа, VAD честно находит в нём фразу — но при
            # этом сессия уже в IDLE, потому что диалог завершился.
            # Переход idle -> transcribing запрещён, и без этой проверки
            # весь контур прослушивания падал намертво: одна фраза,
            # произнесённая вслух, выключала голос до перезапуска.
            if self.session.state is not State.LISTENING:
                log.debug("Фраза длиной %.2f с пришла в состоянии %s — "
                          "вероятно, это голос ассистента",
                          len(utterance) / SAMPLE_RATE, self.session.state.value)
                self._pending_preroll = None
                return

            await self._on_utterance(self._with_preroll(utterance))

    def _take_preroll(self) -> np.ndarray | None:
        """Забрать кольцевой буфер записи в момент начала речи."""
        drain = getattr(self.capture, "drain_preroll", None)
        if drain is None:
            return None
        try:
            data = drain()
        except Exception:
            log.debug("Не удалось забрать pre-roll", exc_info=True)
            return None
        if data is None or getattr(data, "size", 0) == 0:
            return None
        return data

    def _with_preroll(self, utterance: np.ndarray) -> np.ndarray:
        """Приклеить аудио, накопленное до обнаружения речи.

        Сегментер начинает буферизовать только после того, как три кадра
        подряд признаны речью, — то есть самое начало фразы уже минуло.
        На живых фикстурах это стоило слов: «Сделай громкость громче»
        распознавалось как «Громкость громче», а «который час» падал по
        уверенности (0.23 вместо 0.53).

        Перекрытия не будет: буфер кольца заканчивается ровно на моменте
        обнаружения речи, с которого начинается сегмент.
        """
        pre, self._pending_preroll = self._pending_preroll, None
        if pre is None:
            return utterance
        combined = np.concatenate([pre, utterance])
        # SAMPLE_RATE, а не self.sample_rate: у конвейера такого поля нет,
        # и падение в логе убивало бы весь голосовой ввод целиком.
        log.debug("Фраза %.2f с + pre-roll %.2f с = %.2f с",
                  utterance.size / SAMPLE_RATE,
                  pre.size / SAMPLE_RATE,
                  combined.size / SAMPLE_RATE)
        return combined

    def _dump_audio(self, audio: np.ndarray) -> None:
        """Сохранить фразу в wav для разбора.

        Каталог ограничен: без ограничения отладка быстро съела бы
        диск, а фраза длиной в пару секунд весит килограммы за час.
        """
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%H%M%S")
            path = self.record_dir / f"utt_{stamp}_{id(audio) % 10000:04d}.wav"
            write_wav(str(path), audio.astype(np.float32))

            # Храним только последние 40 файлов.
            files = sorted(self.record_dir.glob("utt_*.wav"))
            for old in files[:-40]:
                old.unlink(missing_ok=True)

            log.info("Аудио сохранено: %s (%.2f с)", path, len(audio) / SAMPLE_RATE)
        except Exception:
            log.debug("Не удалось сохранить аудио", exc_info=True)

    def _is_untrustworthy(self, transcript: Transcript) -> bool:
        """Похоже на выдумку модели, а не на услышанную фразу?

        Whisper на шуме и тишине не молчит — сочиняет правдоподобное:
        «субтитры», «спокойная музыка». Именно так в бою появилось
        «Субтитры субтитров Н. Новикова» при попытке открыть блокнот.

        Отсеиваем по уверенности. Измерено на этой модели: живая речь
        0.53-0.92, выдумки 0.00-0.06. Порог 0.25 лежит в разрыве.
        """
        return bool(transcript.text.strip()) and transcript.confidence < self.min_confidence

    async def _on_utterance(self, audio: np.ndarray) -> None:
        turn = self._turn or self.metrics.new_turn()
        self._turn = None
        turn.mark("vad_end")

        await self.bus.emit(EventType.SPEECH_END, duration_s=len(audio) / SAMPLE_RATE)
        await self.session.transition(State.TRANSCRIBING, reason="utterance_complete")

        # Сохраняем ДО распознавания: если движок молчит или выдумывает,
        # без файла причину приходится угадывать по косвенным признакам.
        if self.record_audio:
            self._dump_audio(audio)

        try:
            transcript: Transcript = await self.stt.transcribe(audio)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.errors += 1
            log.exception("Ошибка распознавания")
            await self.bus.emit(EventType.ERROR, where="stt", detail=str(exc))
            await self.session.transition(State.IDLE, reason="stt_error")
            return

        turn.mark("stt_done")
        self.stats.total_stt_time += transcript.elapsed

        if self._is_untrustworthy(transcript):
            # Не отбрасываем молча: в логе должно быть видно, что фраза
            # была, и почему она не стала командой. Иначе «ассистент
            # заглох» — самая трудная для диагностики поломка.
            log.info("Отбрасываю как галлюцинацию: %r (conf=%.2f < %.2f)",
                     transcript.text, transcript.confidence, self.min_confidence)
            self.stats.empty += 1
            await self.bus.emit(EventType.TRANSCRIPT_FINAL, text="",
                                confidence=transcript.confidence,
                                engine=transcript.engine, empty=True,
                                rejected="low_confidence")
            await self.session.transition(State.IDLE, reason="low_confidence")
            return

        log.info("Распознано за %.2f с: %r", transcript.elapsed, transcript.text)

        if transcript.is_empty:
            self.stats.empty += 1
            reason = ("too_quiet" if transcript.rejected == "too_quiet"
                      else "empty_transcript")
            await self.bus.emit(EventType.TRANSCRIPT_FINAL, text="", confidence=0.0,
                                engine=transcript.engine, empty=True,
                                rejected=transcript.rejected)
            await self.session.transition(State.IDLE, reason=reason)
            await self._warn_too_quiet()
            return

        self.stats.utterances += 1
        await self.reset_quiet_warning()

        # ВАЖНО: состояние меняется ДО публикации события, а не после.
        # `bus.publish` ждёт обработчики, и `_on_transcript` отрабатывает
        # весь диалог целиком — включая сброс сессии в IDLE. Если перевести
        # в THINKING после публикации, сессия снова окажется в THINKING
        # уже после завершения диалога, и больше никто из неё не выйдет:
        # активации будут отклоняться («сессия занята») до перезапуска.
        await self.session.transition(State.THINKING, reason="transcript_ready")
        await self.bus.emit(
            EventType.TRANSCRIPT_FINAL,
            text=transcript.text,
            confidence=transcript.confidence,
            engine=transcript.engine,
            stt_elapsed=transcript.elapsed,
            metrics=turn,
        )

    # --- сторож ------------------------------------------------------------

    async def _warn_too_quiet(self) -> None:
        """Один раз говорит, что микрофон молчит.

        Молчание here — худший вариант отказа. Человек нажимает кнопку,
        говорит и не получает ни слова: он не знает, что его слышат, и
        начинает чинить не то (переустанавливать программу, менять
        настройки). Сказанное вслух «я тебя не слышу» сразу ставит на
        правильный след.

        Повторять нельзя: если микрофон молчит постоянно, десять подряд
        произнесённых фраз превратятся в собственный эхо-цикл, который
        самому микрофону и мешает. Поэтому предупреждение одно, а
        дальше — только в лог.
        """
        if self._quiet_warned:
            log.debug("Микрофон молчит, повторно не предупреждаю")
            return
        self._quiet_warned = True
        log.warning("Микрофон молчит: фраза тише порога. "
                    "Проверь вход и уровень записи (run_sinaps.bat check-mic)")
        await self.bus.emit(
            EventType.RESPONSE_READY,
            text="Я тебя не слышу. Проверь микрофон.",
            display="микрофон молчит: фраза слишком тихая",
            source="system")

    async def reset_quiet_warning(self) -> None:
        """Сброс предупреждения — после удачной фразы."""
        self._quiet_warned = False

    async def _watchdog(self) -> None:
        """Снимает сессию, если она «залипла» в нерабочем состоянии.

        Обещание из документации конечного автомата — «THINKING без ответа
        не должен залипать навсегда» — раньше не выполнялось: таймаут был
        только у LISTENING. Любая потерянная транзакция оставляла сессию
        занятой навсегда, и ассистент переставал реагировать до
        перезапуска.

        Страховка, а не основной механизм: правильный порядок переходов
        чинит причину, а этот сторож ловит то, что не поймали.
        """
        while True:
            await asyncio.sleep(1.0)
            try:
                if self.session.busy and self.session.elapsed_in_state > self.stuck_timeout_s:
                    log.warning(
                        "Сессия зависла в %s на %.0f с — сбрасываю",
                        self.session.state.value, self.session.elapsed_in_state)
                    await self.session.reset(reason="watchdog_stuck")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Сторож сессии споткнулся", exc_info=True)

    # --- контекст для ответа (используется конвейером) ----------------------

    def take_turn(self) -> TurnMetrics | None:
        """Забрать метрики текущей фразы (для конвейера понимания)."""
        turn, self._turn = self._turn, None
        return turn
