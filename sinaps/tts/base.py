"""Синтез речи (TTS). Все движки локальные — проект не шлёт текст в сеть.

Обоснование выбора движка (в порядке предпочтения):

1. **Silero v5** — лучший баланс «качество / скорость / размер» для русского.
   Модель ~130 МБ, синтез фразы занимает 30–150 мс на CPU, поэтому не нужна
   видеокарта. Голоса естественнее, чем у системного SAPI5.
2. **Piper** — качественнее Silero, но голоса надо качать отдельно
   (рекомендуется профиль «естественный голос» на v1).
3. **pyttsx3** — системные голоса Windows. Работает всегда, бесплатно, но
   звучит роботом. Оставлен как запасной вариант, чтобы ассистент говорил,
   даже если не загрузилась ни одна нейросетевая модель.

Правило: `speak()` никогда не бросает исключение пользователю. Если движок
упал, ассистент печатает ответ текстом — лучше текст, чем тишина.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import re
import time

import numpy as np

from ..core.errors import EngineUnavailable

log = logging.getLogger(__name__)

#: Символы, которые синтезаторы спотыкаются (тире, кавычки, спецсимволы).
_CHAR_FIXES = {
    "—": ", ", "–": ", ", "…": "...", "«": "", "»": "", "„": "", "“": "",
    "”": "", "*": "", "#": "", "`": "", "~": "", " ": " ",
    "%": " процентов", "&": " и ", "@": " ", "+": " плюс ", "=": " равно ",
}


class TTSEngine(abc.ABC):
    """Контракт синтезатора."""

    name: str = "base"

    def __init__(self, *, volume: float = 1.0, speed: float = 1.0) -> None:
        self.volume = max(0.0, min(1.0, volume))
        self.speed = max(0.5, min(2.0, speed))
        self._warmed = False

    @property
    def sample_rate(self) -> int:
        return 24000

    # --- обязательное ------------------------------------------------------

    @abc.abstractmethod
    def _synthesize(self, text: str) -> np.ndarray:
        """Синтез в float32 mono PCM. Блокирующий вызов -> только из executor."""

    def _load(self) -> None:
        """Ленивая загрузка модели (по умолчанию ничего делать не нужно)."""

    # --- публичное ---------------------------------------------------------

    def warmup(self) -> None:
        if self._warmed:
            return
        started = time.perf_counter()
        try:
            self._synthesize("Проверка связи")
        except Exception as exc:
            log.warning("Прогрев %s не удался: %s", self.name, exc)
        self._warmed = True
        log.info("%s прогрет за %.2f с", self.name, time.perf_counter() - started)

    def synthesize_sync(self, text: str) -> np.ndarray:
        text = self.prepare_text(text)
        if not text:
            return np.zeros(0, dtype=np.float32)
        audio = np.asarray(self._synthesize(text), dtype=np.float32)
        if audio.size and self.volume != 1.0:
            audio = np.clip(audio * self.volume, -1.0, 1.0)
        return audio

    async def synthesize(self, text: str) -> np.ndarray:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.synthesize_sync, text)

    async def speak(self, text: str, player) -> bool:  # noqa: ANN001
        """Синтез + воспроизведение. False — если прервали или нечего говорить."""
        if not (text or "").strip():
            return False
        try:
            pcm = await self.synthesize(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Синтез не удался (%s)", self.name)
            return False
        if pcm.size == 0:
            return False
        try:
            return await player.play(pcm, sample_rate=self.sample_rate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Воспроизведение не удалось: %s", exc)
            return False

    # --- текст -------------------------------------------------------------

    @staticmethod
    def prepare_text(text: str) -> str:
        """Чистит текст перед синтезом.

        Главное правило: нормализовать числа. Синтезатор читает «15» как
        «пятнадцать» или «один пять» в зависимости от модели — приводим к
        словам явно, иначе «открой файл 2» может прозвучать как «два».
        """
        if not text:
            return ""
        for src, dst in _CHAR_FIXES.items():
            text = text.replace(src, dst)
        text = _spell_numbers(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.5, min(2.0, speed))

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(1.0, volume))


_UNITS: list[tuple[int, list[str]]] = []


def number_to_words(num: int) -> str:
    """Число прописью. Живёт в `nlu.slots`, переэкспортируется здесь."""
    from ..nlu.slots import number_to_words as _impl

    return _impl(num)


#: Слова, после которых четырёхзначное число почти наверняка год.
_YEAR_CONTEXT = re.compile(r"^\s*(?:года?|году|г\.|годом|лет)\b", re.IGNORECASE)


def _spell_numbers(text: str) -> str:
    """Заменяет целые числа в тексте на слова.

    Годы оставляем цифрами: «в 2026 году» синтезатор прочитает правильно,
    а «два тысячи двадцать шесть году» — нет.

    Дробные числа («0.5») и номера телефонов/моделей не трогаем: для них
    есть отдельные правила, которых пока нет, поэтому оставляем как есть.
    """

    def repl(match: re.Match) -> str:
        raw = match.group(0)
        after = text[match.end():]

        # `\`b` не ловит границу, если рядом точка/запятая — проверяем вручную.
        before = text[:match.start()]
        if before and before[-1].isdigit():
            return raw
        if after and after[0].isdigit():
            return raw
        if "." in after[:3] or "," in after[:3]:  # дробное число
            return raw

        try:
            value = int(raw)
        except ValueError:
            return raw

        if abs(value) > 999_999:
            return raw  # слишком длинно — пусть движок сам прочитает
        if len(raw) == 4 and _YEAR_CONTEXT.match(after):
            return raw  # год

        return number_to_words(value)

    return re.sub(r"\d+", repl, text)


def load_engine(name: str, **kwargs) -> TTSEngine:
    """Фабрика синтезаторов по имени из конфига."""
    name = (name or "").lower()

    if name in ("silero", "silero_tts"):
        from .silero_tts import SileroTTS

        return SileroTTS(**kwargs)
    if name == "piper":
        from .piper_tts import PiperTTS

        return PiperTTS(**kwargs)
    if name in ("pyttsx3", "sapi", "system"):
        from .pyttsx3_tts import Pyttsx3TTS

        return Pyttsx3TTS(**kwargs)
    if name in ("", "none", "off"):
        from .null_tts import NullTTS

        return NullTTS(**kwargs)

    raise EngineUnavailable(name, "Допустимо: silero, piper, pyttsx3, none")
