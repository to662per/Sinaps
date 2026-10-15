"""Выбор и диагностика аудиоустройств.

Здесь же — единственное место, где живёт прямой импорт `sounddevice`.
Остальной код работает через `AudioCapture`/`AudioPlayer`, поэтому
подмена бэкенда (например, на WASAPI-эксклюзив или на файл) не затрагивает
логику.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import sounddevice as sd

from ..core.errors import AudioError, NoAudioDevice

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    channels: int
    default_samplerate: float
    is_input: bool

    def __str__(self) -> str:
        kind = "вход" if self.is_input else "выход"
        return f"[{self.index:>2}] {kind:>4}  {self.name}  ({self.default_samplerate:.0f} Гц)"


def list_devices(show_all: bool = False) -> list[DeviceInfo]:
    """Все устройства Portaudio. `show_all=False` — только используемые."""
    devices: list[DeviceInfo] = []
    for idx, dev in enumerate(sd.query_devices()):
        if not show_all and dev["max_input_channels"] == 0 and dev["max_output_channels"] == 0:
            continue
        devices.append(
            DeviceInfo(
                index=idx,
                name=dev["name"],
                channels=max(dev["max_input_channels"], dev["max_output_channels"]),
                default_samplerate=float(dev["default_samplerate"]),
                is_input=dev["max_input_channels"] > 0,
            )
        )
    return devices


def list_input_devices() -> list[tuple[Any, str]]:
    """Микрофоны для выпадающего списка: (индекс, имя).

    Именно такой вид ожидает окно настроек. Обёртка существует отдельно
    от `list_devices` не для удобства, а ради границы: список устройств
    имеет право знать про Portaudio, а окно — нет, иначе смена бэкенда
    аудио потянула бы за собой правку интерфейса.
    """
    return [(d.index, d.name) for d in list_devices() if d.is_input]


def list_output_devices() -> list[tuple[Any, str]]:
    """Динамики для выпадающего списка: (индекс, имя)."""
    return [(d.index, d.name) for d in list_devices() if not d.is_input]


def _default_device_indices() -> tuple[int | None, int | None]:
    """Индексы устройств по умолчанию.

    В sounddevice 0.4 было `sd.default_device`, в 0.5 — только `sd.default.device`.
    Поддерживаем оба варианта, иначе на свежей версии всё падает.
    """
    legacy = getattr(sd, "default_device", None)
    if legacy is not None:
        inp, out = legacy
        return inp, out
    try:
        dev = sd.default.device
        return dev[0], dev[1]
    except Exception:
        return None, None


def print_devices() -> None:
    default_in, default_out = _default_device_indices()

    print("Аудиоустройства:\n")
    for dev in list_devices(show_all=True):
        marks = []
        if dev.is_input and dev.index == default_in:
            marks.append("микрофон по умолчанию")
        if not dev.is_input and dev.index == default_out:
            marks.append("динамики по умолчанию")
        suffix = f"  <- {', '.join(marks)}" if marks else ""
        print(f"  {dev}{suffix}")

    inputs = [d for d in list_devices() if d.is_input]
    if not inputs:
        print("\n  ВНИМАНИЕ: устройств ввода не найдено.")
        print("  Проверь: подключён ли микрофон, включён ли в настройках Windows,")
        print("  не захвачен ли он другой программой.")
        print("  Работать можно и без микрофона: используй текстовый ввод в консоли.")


def resolve_device(spec: str | int | None, *, want_input: bool) -> int | None:
    """Превращает настройку в индекс устройства.

    `spec` может быть: None (системное умолчание), int (индекс), либо строка —
    тогда ищем по подстроке имени, регистронезависимо.
    """
    if spec is None or spec == "":
        return None

    if isinstance(spec, int):
        return spec

    needle = str(spec).lower().strip()
    candidates = [d for d in list_devices() if needle in d.name.lower()]
    if not candidates:
        raise AudioError(f"Аудиоустройство '{spec}' не найдено")
    match = next((d for d in candidates if d.is_input == want_input), candidates[0])
    if not match.is_input == want_input:
        log.warning("Устройство '%s' не подходит как %s, беру его же",
                    spec, "микрофон" if want_input else "динамики")
    log.info("Выбрано устройство: %s", match)
    return match.index


def probe_input(device: str | int | None = None, sample_rate: int = 16000) -> float:
    """Быстро проверяет, что с микрофона идёт сигнал. Возвращает RMS.

    Нужен для `--selftest` и для диагностики «ассистент меня не слышит».
    """
    idx = resolve_device(device, want_input=True)
    frames = int(sample_rate * 0.5)
    try:
        with sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=idx,
            blocksize=frames,
        ) as stream:
            data, _ = stream.read(frames)
    except Exception as exc:
        raise NoAudioDevice(str(exc)) from exc

    rms = float(np.sqrt(np.mean(np.square(data))))
    log.info("Уровень входного сигнала: RMS=%.5f", rms)
    return rms


def default_sample_rate(device: str | int | None = None, want_input: bool = True) -> int:
    idx = resolve_device(device, want_input=want_input)
    if idx is None:
        info = sd.query_devices(kind="input" if want_input else "output")
        return int(info["default_samplerate"])
    return int(sd.query_devices(idx)["default_samplerate"])


def has_input_device() -> bool:
    """Есть ли вообще микрофон. Проверяется без открытия потока."""
    return any(d.is_input for d in list_devices())
