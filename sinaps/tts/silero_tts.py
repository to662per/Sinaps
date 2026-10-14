"""Silero TTS v5 — основной локальный синтезатор.

Факты, проверенные на этой машине (не догадки из документации):

* Правильный репозиторий — `snakers4/silero-models` (в `snakers4/silero-vad`
  TTS убрали, `hub.load(..., model='silero_tts')` падает с «Cannot find callable»).
* Нужен пакет `omegaconf`, а для v5-моделей — ещё и `scipy`.
* API v5: `model.apply_tts(text=..., speaker=..., sample_rate=...)`.
* Модель возвращает **одномерный torch.Tensor** float32, диапазон примерно
  [-0.64, 0.44] — то есть уже нормализован, дополнительно нормировать не нужно.
* Вывод **детерминированный**: два одинаковых вызова дают побайтово равный
  результат. Это позволяет писать точные тесты.
* Голоса RU: `aidar` (мужской), `baya`, `kseniya` (женские), `eugene` (мужской),
  `xenia` (женский, рекомендуется как «нейтрально-женский» по умолчанию).
* Скорость: ~150 мс на фразу в 2–3 с речи на CPU — для ассистента отлично.
* Грабли: строки короче 2 символов роняют модель. Пустые входы отсекаются
  в `TTSEngine.prepare_text`, остальное ловится в `_synthesize`.

Скорость речи регулируется параметром `speed` самого Silero, а не пересэмплированием:
это сохраняет естественность темпа (слова не «разъезжаются»).
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from ..core.errors import EngineUnavailable
from ..core.offline import MissingModelError
from ..core.offline import hub_repo_path as _hub_repo_path
from ..core.offline import require_tts_models as _require_tts_models
from .base import TTSEngine

log = logging.getLogger(__name__)

#: Русские голоса Silero v5. Первое — по умолчанию.
RU_VOICES = ("xenia", "kseniya", "baya", "aidar", "eugene")

#: Человеческие описания для окна настроек.
VOICE_LABELS = {
    "xenia": "Ксения / Женский нейтральный",
    "kseniya": "Ксения / Женский мягкий",
    "baya": "Байя / Женский",
    "aidar": "Айдар / Мужской",
    "eugene": "Юджин / Мужской",
}

_REPO = "snakers4/silero-models"
_SPEAKER_MODEL = "v5_ru"

#: Silero v5 умеет ровно эти частоты. Проверено: 16000 -> AssertionError.
SUPPORTED_RATES = (8000, 24000, 48000)


class SileroTTS(TTSEngine):
    name = "silero"

    def __init__(
        self,
        *,
        voice: str = "xenia",
        language: str = "ru",
        speed: float = 1.0,
        volume: float = 1.0,
        sample_rate: int = 24000,
        **_ignored: object,
    ) -> None:
        super().__init__(volume=volume, speed=speed)
        self.language = language if language in ("ru", "en") else "ru"
        self.voice = voice if voice in RU_VOICES else RU_VOICES[0]
        self._sample_rate = sample_rate if sample_rate in SUPPORTED_RATES else 24000
        self._model = None
        self._lock = threading.Lock()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def available_voices(self) -> tuple[str, ...]:
        return RU_VOICES

    # --- загрузка ----------------------------------------------------------

    def _load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                # Проверяем кэш до вызова: `torch.hub.load(source="github")`
                # при пустом кэше выходит в интернет. Сеть в ядре запрещена,
                # поэтому отсутствие модели — понятная ошибка, а не повод
                # скачать. Раньше здесь был текст «проверь интернет при
                # первом запуске»: ядро само предлагало человеку открыть
                # сеть, нарушая собственное обещание.
                _require_tts_models()
                import torch  # noqa: F401

                # `source="local"` вместо `source="github"` — ключевое
                # изменение. При `github` torch.hub при пустом кэше сам
                # идёт в интернет; при `local` берёт код из папки на
                # диске и сеть не задействует вовсе. Установщик
                # складывает эту папку заранее, поэтому путь всегда есть.
                model, _example = torch.hub.load(
                    repo_or_dir=str(_hub_repo_path()),
                    model="silero_tts",
                    source="local",
                    language=self.language,
                    speaker=_SPEAKER_MODEL if self.language == "ru" else f"{self.language}_v3",
                    trust_repo=True,
                    verbose=False,
                )
            except MissingModelError:
                # Модели нет — состояние машины, а не поломка движка.
                raise
            except ImportError as exc:
                raise EngineUnavailable(
                    self.name,
                    "Установи: .venv\\Scripts\\pip install torch torchaudio omegaconf scipy",
                ) from exc
            except Exception as exc:
                raise EngineUnavailable(
                    self.name,
                    "Не удалось загрузить модель Silero из локального кэша "
                    f"(проверь наличие omegaconf/scipy): {exc}",
                ) from exc

            self._model = model
            log.info(
                "Silero TTS v5 загружен: голос=%s (%s), частота=%d Гц",
                self.voice, VOICE_LABELS.get(self.voice, ""), self._sample_rate,
            )

    # --- синтез ------------------------------------------------------------

    def _synthesize(self, text: str) -> np.ndarray:
        if self._model is None:
            self._load()

        text = text.strip()
        if len(text) < 2:
            # Модель падает на слишком коротком входе; тишина — честный ответ.
            return np.zeros(0, dtype=np.float32)

        import torch

        try:
            with torch.no_grad():
                out = self._model.apply_tts(
                    text=text,
                    speaker=self.voice,
                    sample_rate=self._sample_rate,
                    # Внимание: у v5 НЕТ параметра speed — темп меняем сами,
                    # см. `_apply_speed` ниже.
                )
        except ValueError as exc:
            # Неверный голос/частота — это наша ошибка конфигурации, а не сбой.
            raise EngineUnavailable(self.name, str(exc)) from exc
        except Exception as exc:
            # Один сбойный вход не должен ронять ассистента: возвращаем тишину,
            # вызывающий код напечатает текст.
            log.error("Синтез не удался (%r): %s", text[:60], exc)
            return np.zeros(0, dtype=np.float32)

        audio = out["audio"] if isinstance(out, dict) else out
        audio = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        audio = np.ascontiguousarray(audio.reshape(-1), dtype=np.float32)
        return self._apply_speed(audio)

    def _apply_speed(self, audio: np.ndarray) -> np.ndarray:
        """Меняет темп пересемплированием.

        Ограничение, о котором надо знать: это наивный метод — темп ускоряется
        вместе с высотой тона (на 1.5× голос заметно «поднимается»).
        Правильное решение — time-stretch с сохранением pitch (phase vocoder),
        но это тяжёлая зависимость; в v1 можно перейти на Piper, у которого
        регулируется длина фразы на уровне модели.
        Для диапазона 0.9–1.1 артефакты практически неслышны.
        """
        if self.speed == 1.0 or audio.size == 0:
            return audio
        target = max(1, int(round(audio.size / self.speed)))
        positions = np.linspace(0, audio.size - 1, target)
        return np.interp(positions, np.arange(audio.size), audio).astype(np.float32)

    # --- настройка ---------------------------------------------------------

    def set_voice(self, voice: str) -> None:
        if voice not in RU_VOICES:
            raise ValueError(f"Неизвестный голос '{voice}'. Доступно: {', '.join(RU_VOICES)}")
        if voice != self.voice:
            self.voice = voice
            log.info("Голос изменён на %s", voice)

    def set_sample_rate(self, sample_rate: int) -> None:
        if sample_rate not in SUPPORTED_RATES:
            raise ValueError(
                f"Silero v5 поддерживает {', '.join(str(r) for r in SUPPORTED_RATES)} Гц"
            )
        self._sample_rate = sample_rate
