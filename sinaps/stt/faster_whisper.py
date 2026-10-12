"""STT на faster-whisper (CTranslate2) — основной локальный движок.

Почему faster-whisper, а не vosk:
  * на порядок точнее на русском, особенно на коротких командах;
  * умеет GPU через CTranslate2 — на RTX 4060 фраза распознаётся за ~0.3–0.6 с;
  * работает офлайн, модель скачивается один раз (~150 МБ для small, ~1.5 ГБ для large-v3-turbo).

Автоматический выбор compute_type — ключ к скорости:
  * CUDA + float16 — лучшее качество и скорость, но требует ~2 ГБ VRAM;
  * CPU + int8 — работает везде, на 4-8 ядрах фраза 4 с ≈ 1–2 с.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import numpy as np

from ..core.config import PROJECT_ROOT
from ..core.errors import EngineUnavailable
from .base import STTEngine, Transcript

log = logging.getLogger(__name__)

#: Модели по имени -> размер на диске (примерно, для выбора на слабом железе)
MODEL_HINTS = {
    "tiny": "75 МБ, самая быстрая, низкая точность на русском",
    "base": "140 МБ, быстрая, точность средняя",
    "small": "460 МБ, быстро, но на живом микрофоне путает слова",
    "medium": "1.5 ГБ, хорошая точность, нужна GPU",
    "large-v3-turbo": "1.6 ГБ, лучшая точность, нужна GPU 8+ ГБ — выбор по умолчанию",
}


#: Признаки того, что ошибка связана именно с видеокартой, а не с аудио/моделью.
_CUDA_ERROR_MARKERS = (
    "cublas", "cudnn", "cuda", "gpu", "out of memory", "outofmemory",
    "nvrtc", "device", "driver",
)


def _is_cuda_error(exc: BaseException) -> bool:
    """Отличать GPU-сбой от прочих ошибок: откатываться стоит не на всё подряд."""
    message = str(exc).lower()
    return any(marker in message for marker in _CUDA_ERROR_MARKERS)


#: Хэндеры os.add_dll_directory. Их нужно хранить: если сборщик мусора
#: уничтожит объект, директория исчезнет из пути поиска DLL и снова сломается.
_DLL_HANDLES: list = []


def _enable_cuda_dlls() -> bool:
    """Регистрирует папки с CUDA-DLL в путях поиска Windows.

    Зачем это нужно. `nvidia-cublas-cu12` и `nvidia-cudnn-cu12` кладут
    библиотеки в `site-packages/nvidia/*/bin`, а Windows ищет зависимые DLL
    только в системных каталогах и в путях, добавленных через
    `os.add_dll_directory` (обычный `PATH` Python 3.8+ игнорирует).

    Без этого вылезает: `Library cublas64_12.dll is not found or cannot be
    loaded` — при том что видеокарта есть и `get_cuda_device_count()` == 1.

    Проверено на Windows 11 + RTX 4060: DLL грузится по абсолютному пути,
    но CTranslate2 без этой регистрации её не находит.
    """
    import sysconfig

    if not sys.platform.startswith("win"):
        return True  # на Linux/macOS достаточно обычного PATH

    site_root = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    if not site_root.exists():
        return False

    added = []
    for bindir in site_root.glob("*/bin"):
        if not bindir.exists():
            continue
        try:
            _DLL_HANDLES.append(os.add_dll_directory(str(bindir)))
            added.append(str(bindir))
        except OSError as exc:
            log.debug("Не удалось добавить %s: %s", bindir, exc)

    if added:
        # CTranslate2 — C++-расширение, оно вызывает LoadLibrary напрямую.
        # Такой поиск идёт по стандартному порядку Windows, куда входит PATH,
        # поэтому дублируем директории ещё и в переменную окружения.
        os.environ["PATH"] = os.pathsep.join(added + [os.environ.get("PATH", "")])
        log.debug("CUDA DLL зарегистрированы: %s", ", ".join(added))
        return True
    return False


class FasterWhisperEngine(STTEngine):
    name = "faster_whisper"

    def __init__(
        self,
        *,
        model: str = "small",
        device: str = "auto",
        compute_type: str = "auto",
        language: str = "ru",
        beam_size: int = 1,
        initial_prompt: str = "",
        download_root: str | None = None,
        normalize_audio: bool = True,
        min_dbfs: float = -50.0,
    ) -> None:
        super().__init__(language=language, normalize_audio=normalize_audio,
                         min_dbfs=min_dbfs)
        self.model_name = model
        self.requested_device = device
        self.requested_compute_type = compute_type
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt
        # Куда складывать веса модели. None -> models/ рядом с проектом.
        self.download_root = download_root or str(PROJECT_ROOT / "models")
        self._model = None
        self._device = "cpu"
        self._compute_type = "int8"
        # Флаг «GPU definitively не работает». Нужен, потому что после отката
        # `_detect_device()` снова выбрал бы cuda из device="auto" — и мы бы
        # падали по кругу.
        self._force_cpu = False

    # --- запуск ------------------------------------------------------------

    def _detect_device(self) -> tuple[str, str]:
        """Возвращает (device, compute_type) с учётом железа и настроек."""
        if self._force_cpu:
            return "cpu", "int8"

        device = self.requested_device
        if device == "auto":
            device = "cuda" if self._has_cuda() else "cpu"

        if self.requested_compute_type != "auto":
            return device, self.requested_compute_type

        if device == "cuda":
            # float16 на GPU — лучший баланс скорости и точности.
            # На маленьких моделях CTranslate2 быстрее работает в int8_float16.
            compute = "float16" if self.model_name in ("medium", "large-v3-turbo") else "int8_float16"
        else:
            compute = "int8"  # на CPU int8 в 2–4 раза быстрее float32
        return device, compute

    @staticmethod
    def _has_cuda() -> bool:
        try:
            import ctranslate2

            return ctranslate2.get_cuda_device_count() > 0
        except Exception:
            return False

    def _switch_to_cpu(self, reason: str) -> None:
        """Аварийный откат на CPU.

        Отдельный метод, потому что CUDA-ошибки возникают в двух разных местах:
        при создании модели (нет `cublas64_12.dll`) и при первом же запросе.
        Ловить только загрузку недостаточно — модель создастся, а упадёт инференс.
        """
        if self._device == "cpu":
            # Уже на CPU: либо его выбрали в конфиге, либо мы туда откатились.
            # Повторный откат не нужен и лишь зациклил бы пересоздание модели.
            return
        log.warning("GPU недоступен (%s) — переключаюсь на CPU", reason)
        self._device, self._compute_type = "cpu", "int8"
        self._force_cpu = True
        self._model = None  # пересоздастся при следующем _load()

    def _load(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise EngineUnavailable(
                self.name,
                "Установи: .venv\\Scripts\\pip install faster-whisper",
            ) from exc

        device, compute = self._detect_device()

        if device == "cuda" and not _enable_cuda_dlls():
            log.warning(
                "Не нашёл CUDA-библиотеки (site-packages/nvidia/*/bin). "
                "Установи nvidia-cublas-cu12 и nvidia-cudnn-cu12, иначе будет CPU."
            )

        try:
            self._model = WhisperModel(
                self.model_name,
                device=device,
                compute_type=compute,
                download_root=self.download_root,
                cpu_threads=0,  # 0 = автоматически по числу ядер
                num_workers=1,
            )
        except Exception as exc:
            # Самая частая ошибка у новичков: нет CUDA-библиотек или не хватает
            # VRAM. Откатываемся на CPU — лучше медленно, чем не работает.
            if device == "cuda":
                self._switch_to_cpu(str(exc))
                device, compute = "cpu", "int8"
                try:
                    self._model = WhisperModel(
                        self.model_name, device=device, compute_type=compute,
                        download_root=self.download_root,
                    )
                except Exception as exc2:
                    raise EngineUnavailable(self.name, str(exc2)) from exc2
            else:
                raise EngineUnavailable(self.name, str(exc)) from exc

        self._device, self._compute_type = device, compute
        log.info(
            "faster-whisper готов: модель=%s, устройство=%s, точность=%s (%s)",
            self.model_name, device, compute, MODEL_HINTS.get(self.model_name, ""),
        )

    # --- распознавание -----------------------------------------------------

    @property
    def device(self) -> str:
        return self._device

    def _recognize(self, audio: np.ndarray) -> Transcript:
        if self._model is None:
            self._load()

        if audio.size == 0:
            return Transcript(text="", confidence=0.0, engine=self.name)

        # Whisper ждёт float32; приводим к нужной частоте один раз.
        pcm = np.ascontiguousarray(audio, dtype=np.float32)

        try:
            return self._run(pcm)
        except Exception as exc:
            # GPU-ошибки часто всплывают именно здесь, а не при загрузке:
            # модель создаётся, а падает первая же инференция (нет cublas,
            # не хватило VRAM, несовместимый драйвер). Откатываемся на CPU
            # и повторяем ровно один раз.
            if self._device != "cuda" or not _is_cuda_error(exc):
                raise
            log.warning("Сбой GPU при распознавании: %s", exc)
            self._switch_to_cpu(str(exc))
            self._load()
            return self._run(pcm)

    def _run(self, pcm: np.ndarray) -> Transcript:
        segments, info = self._model.transcribe(
            pcm,
            language=self.language,
            beam_size=self.beam_size,
            best_of=1,
            vad_filter=False,          # границы уже определил наш VAD
            condition_on_previous_text=False,  # фразы независимы — быстрее и без эха
            initial_prompt=self.initial_prompt or None,
            without_timestamps=True,
        )

        text_parts: list[str] = []
        logprobs: list[float] = []
        no_speech: list[float] = []

        for seg in segments:  # генератор: тянуть нужно обязательно
            text_parts.append(seg.text)
            if seg.avg_logprob is not None:
                logprobs.append(seg.avg_logprob)
            if getattr(seg, "no_speech_prob", None) is not None:
                no_speech.append(seg.no_speech_prob)

        text = " ".join(p.strip() for p in text_parts if p.strip()).strip()
        confidence = self._confidence(logprobs, no_speech, text)

        log.debug("Распознано: %r (conf=%.2f)", text, confidence)
        return Transcript(
            text=text,
            confidence=confidence,
            is_final=True,
            language=getattr(info, "language", self.language),
            engine=self.name,
        )

    @staticmethod
    def _confidence(
        logprobs: list[float], no_speech: list[float], text: str
    ) -> float:
        if not text:
            return 0.0
        avg = sum(logprobs) / len(logprobs) if logprobs else -0.5
        ns = max(no_speech) if no_speech else 0.0
        return STTEngine.estimate_confidence(avg, ns)
