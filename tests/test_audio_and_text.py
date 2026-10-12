"""Тесты аудио-утилит и TTS-подготовки текста.

Здесь нет ни микрофона, ни моделей: только чистые функции, которые легко
сломать и легко проверить.
"""

from __future__ import annotations

import numpy as np
import pytest

from sinaps.audio.capture import (
    dbfs,
    normalize_peak,
    read_wav,
    resample,
    rms,
    to_int16,
    trim_silence,
    write_wav,
)
from sinaps.tts.base import TTSEngine, number_to_words

SR = 16000


def _tone(seconds: float, freq: float = 440.0, amp: float = 0.5) -> np.ndarray:
    t = np.linspace(0, seconds, int(SR * seconds), endpoint=False)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# --- громкость и нормализация ----------------------------------------------


def test_rms_of_silence_is_zero():
    assert rms(np.zeros(1000, dtype=np.float32)) == 0.0


def test_rms_of_constant_signal():
    signal = np.full(1000, 0.5, dtype=np.float32)
    assert rms(signal) == pytest.approx(0.5, rel=1e-5)


def test_normalize_peak_reaches_target():
    quiet = _tone(0.5, amp=0.01)
    result = normalize_peak(quiet, target=0.95)
    assert float(np.max(np.abs(result))) == pytest.approx(0.95, rel=1e-3)


def test_normalize_peak_leaves_empty_and_silent_alone():
    assert normalize_peak(np.zeros(0, dtype=np.float32)).size == 0
    # Полная тишина не должна превратиться в шум из-за деления на ноль.
    silence = np.zeros(100, dtype=np.float32)
    assert np.all(normalize_peak(silence) == 0)


def test_normalize_peak_never_exceeds_one():
    loud = _tone(0.5, amp=1.0)
    assert float(np.max(np.abs(normalize_peak(loud)))) <= 1.0


def test_dbfs_scale():
    assert dbfs(np.zeros(1000, dtype=np.float32)) < -100
    half = np.full(1000, 0.5, dtype=np.float32)
    assert dbfs(half) == pytest.approx(-6.02, abs=0.05)


# --- обрезка тишины ---------------------------------------------------------


def test_trim_silence_removes_leading_and_trailing():
    speech = _tone(0.5, amp=0.5)
    padded = np.concatenate([
        np.zeros(int(SR * 0.3), dtype=np.float32),   # тишина в начале
        speech,
        np.zeros(int(SR * 0.4), dtype=np.float32),   # тишина в конце
    ])
    trimmed = trim_silence(padded, sample_rate=SR)
    assert trimmed.size < padded.size

    # Речь сохраняется целиком плюс отступы по 150 мс с каждой стороны.
    # Отступ вырос с 50 до 150 мс намеренно: первые 100-300 мс слова
    # тихие, и при малом отступе они попадали под нож — «Сделай громче»
    # превращалось в «Громче». Лишние 0.3 с на скорость распознавания
    # не влияют, а вот потеря слова — это потеря команды.
    margin = int(SR * 0.15)
    assert abs(trimmed.size - (speech.size + 2 * margin)) < int(SR * 0.05)


def test_trim_silence_margin_protects_soft_onset():
    """Начало фразы тише порога должно выжить."""
    # 200 мс тихого подхода + 500 мс речи.
    quiet = _tone(0.2, amp=0.004)
    speech = _tone(0.5, amp=0.5)
    combined = np.concatenate([quiet, speech])

    trimmed = trim_silence(combined, sample_rate=SR)

    # Хотя бы половина тихого подхода должна сохраниться.
    assert trimmed.size > speech.size * 0.8, (
        f"тихое начало срезано: осталось {trimmed.size/SR:.2f}с "
        f"из {combined.size/SR:.2f}с")


def test_trim_silence_keeps_signal_without_padding():
    speech = _tone(1.0, amp=0.5)
    trimmed = trim_silence(speech, sample_rate=SR)
    assert trimmed.size == pytest.approx(speech.size, rel=0.05)


def test_trim_silence_on_empty():
    assert trim_silence(np.zeros(0, dtype=np.float32)).size == 0


# --- пересемплирование ------------------------------------------------------


def test_resample_changes_length_proportionally():
    audio = _tone(1.0)
    up = resample(audio, SR, 24000)
    assert up.size == pytest.approx(int(audio.size * 1.5), rel=0.01)


def test_resample_same_rate_is_noop():
    audio = _tone(0.1)
    assert resample(audio, SR, SR) is audio


def test_resample_preserves_zero_crossings():
    """Пересемплирование не должно ломать фазу: пики остаются на месте."""
    audio = _tone(0.5, freq=100.0)
    resampled = resample(audio, SR, 24000)
    peak_before = int(np.argmax(np.abs(audio[:2000])))
    peak_after = int(np.argmax(np.abs(resampled[:6000])))
    assert peak_after == pytest.approx(peak_before * 1.5, abs=2)


# --- WAV --------------------------------------------------------------------


def test_wav_roundtrip(tmp_path):
    audio = _tone(0.25)
    path = tmp_path / "test.wav"
    write_wav(str(path), audio, SR)
    loaded, rate = read_wav(str(path))
    assert rate == SR
    # int16-квантование даёт небольшую погрешность — это ожидаемо.
    assert np.max(np.abs(loaded - audio)) < 0.01


def test_to_int16_clips_out_of_range():
    audio = np.array([-2.0, 0.0, 2.0], dtype=np.float32)
    pcm = to_int16(audio)
    assert pcm[0] == -32767
    assert pcm[2] == 32767
    assert pcm[1] == 0


# --- числа прописью ---------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "ноль"),
        (1, "один"),
        (2, "два"),
        (5, "пять"),
        (10, "десять"),
        (11, "одиннадцать"),
        (15, "пятнадцать"),
        (20, "двадцать"),
        (21, "двадцать один"),
        (30, "тридцать"),
        (42, "сорок два"),
        (99, "девяносто девять"),
        (100, "сто"),
        (101, "сто один"),
        (111, "сто одиннадцать"),
        (125, "сто двадцать пять"),
        (200, "двести"),
        (345, "триста сорок пять"),
        (999, "девятьсот девяносто девять"),
        (1000, "одна тысяча"),
        (1500, "одна тысяча пятьсот"),
        (2000, "две тысячи"),
        (5000, "пять тысяч"),
    ],
)
def test_number_to_words(value, expected):
    assert number_to_words(value) == expected


def test_number_to_words_negative():
    assert number_to_words(-5) == "минус пять"


# --- подготовка текста к синтезу -------------------------------------------


def test_prepare_text_removes_punctuation_noise():
    out = TTSEngine.prepare_text("Открываю — блокнот… «прямо» сейчас")
    assert "—" not in out and "…" not in out and "«" not in out


def test_prepare_text_spells_numbers():
    out = TTSEngine.prepare_text("Поставь таймер на 15 минут")
    assert "15" not in out
    assert "пятнадцать" in out


def test_prepare_text_keeps_years_as_digits():
    """Год нельзя читать «две тысячи двадцать шесть» — оставляем цифрами."""
    out = TTSEngine.prepare_text("В 2026 году")
    assert "2026" in out


def test_prepare_text_empty():
    assert TTSEngine.prepare_text("") == ""
    assert TTSEngine.prepare_text("   ") == ""


def test_prepare_text_collapses_spaces():
    out = TTSEngine.prepare_text("привет    мир")
    assert out == "привет мир"
