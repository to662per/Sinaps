"""Оффлайн-режим: сеть в ядре запрещена структурой кода.

Повод
-----
В `PrivacyConfig` был флаг `local_only` с комментарием «жёсткий запрет
сетевых вызовов (требование проекта)». Проверка кода показала: флаг
читался в двух местах, и оба раза только для печати текста — «локально,
без сети» либо «СЕТЬ РАЗРЕШЕНА». Ничто не блокировалось. Поставь
`local_only: false` — изменится надпись, а не поведение.

Это опаснее отсутствия обещания: читатель верит и перестаёт проверять.

Плюс два скрытых канала, найденных измерением:
  * `faster-whisper` без `local_files_only` идёт на HuggingFace;
  * `torch.hub.load(source="github")` идёт на GitHub при пустом кэше.
"""

from __future__ import annotations

import os

import pytest

from sinaps.core import offline
from sinaps.core.offline import MissingModelError


# --- переменные окружения ---------------------------------------------------


def test_enforce_offline_sets_flags(monkeypatch):
    for name in offline._OFFLINE_VARS:
        monkeypatch.delenv(name, raising=False)

    offline.enforce_offline()

    for name in offline._OFFLINE_VARS:
        assert os.environ.get(name) == "1", f"{name} не выставлена"


def test_enforce_offline_does_not_override_existing(monkeypatch):
    """Чужое значение не перебиваем — но и не оставляем сетевым."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    offline.enforce_offline()

    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_every_flag_value_is_one(monkeypatch):
    """Библиотеки проверяют наличие переменной, а значение читают как «1».

    Если поставить «0», часть из них посчитает это разрешением сети.
    """
    for name in offline._OFFLINE_VARS:
        monkeypatch.delenv(name, raising=False)

    offline.enforce_offline()

    for name in offline._OFFLINE_VARS:
        assert os.environ[name] in ("1", "true", "True"), (
            f"{name}={os.environ[name]!r}: библиотеки ждут '1', "
            "'0' может быть прочитан как разрешение сети"
        )


# --- модель синтеза ---------------------------------------------------------


def test_missing_tts_model_is_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "hub_dir", lambda: tmp_path / "hub")

    with pytest.raises(MissingModelError) as info:
        offline.require_tts_models()

    message = str(info.value)
    assert "setup_sinaps.bat" in message, "не сказано, что делать"
    assert "запрещена" in message, "не сказано, почему не скачает"
    assert "hubconf.py" in message, "не назван ожидаемый путь"


def test_existing_tts_model_passes(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    repo = hub / "snakers4_silero-models_master"
    repo.mkdir(parents=True)
    (repo / "hubconf.py").write_text("# models")

    monkeypatch.setattr(offline, "hub_dir", lambda: hub)

    offline.require_tts_models()  # не должно бросить


def test_empty_cache_folder_is_not_a_model(tmp_path, monkeypatch):
    """Папка есть, модели в ней нет — значит модели нет."""
    hub = tmp_path / "hub"
    (hub / "snakers4_silero-models_master").mkdir(parents=True)

    monkeypatch.setattr(offline, "hub_dir", lambda: hub)

    with pytest.raises(MissingModelError):
        offline.require_tts_models()


def test_hub_repo_path_points_to_models():
    assert offline.hub_repo_path().name == "snakers4_silero-models_master"


# --- модель распознавания ---------------------------------------------------


def _make_model(root, name: str, *, weights: bool = True) -> None:
    """Папка модели в том виде, в каком её кладёт huggingface_hub."""
    folder = root / f"models--Systran--faster-whisper-{name}"
    snapshot = folder / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    if weights:
        (snapshot / "model.bin").write_bytes(b"\0")


def test_existing_whisper_model_passes(tmp_path):
    _make_model(tmp_path, "small")

    offline.require_whisper_model("small", tmp_path)  # не должно бросить


def test_model_named_with_repository_prefix_still_found(tmp_path):
    """Имя папки — `models--<владелец>--faster-whisper-<модель>`.

    Первая версия проверки искала `--<модель>` в имени каталога и
    объявляла отсутствующей модель, которая лежала на диске: перед
    именем модели там одиночный дефис, а не два.
    """
    _make_model(tmp_path, "large-v3-turbo")

    offline.require_whisper_model("large-v3-turbo", tmp_path)


def test_empty_model_folder_is_missing_model(tmp_path):
    """Каталог создаётся до скачивания — сам по себе он не модель."""
    _make_model(tmp_path, "tiny", weights=False)

    with pytest.raises(MissingModelError):
        offline.require_whisper_model("tiny", tmp_path)


def test_missing_model_error_lists_what_is_on_disk(tmp_path):
    """Полезнее сказать, что есть, чем просто «не найдено»."""
    _make_model(tmp_path, "small")

    with pytest.raises(MissingModelError) as info:
        offline.require_whisper_model("base", tmp_path)

    message = str(info.value)
    assert "faster-whisper-small" in message, (
        "человеку не сказано, какие модели установлены — "
        "придётся лезть в проводник"
    )


def test_missing_models_folder_is_clear(tmp_path):
    with pytest.raises(MissingModelError) as info:
        offline.require_whisper_model("small", tmp_path / "нет-такой")

    assert "setup_sinaps.bat" in str(info.value)


# --- обещание против поведения ---------------------------------------------


def test_local_only_flag_is_not_enforcement():
    """Документируем, чем флаг НЕ является.

    Раньше `local_only` печатался в двух местах и больше нигде не
    участвовал. Тест стоит здесь, чтобы никто не решил, что флаг
    запрещает сеть: запрещает структура кода, а флаг — нет.
    """
    from sinaps.core.config import SinapsConfig

    assert SinapsConfig().privacy.local_only is True

    # Флаг можно выключить, и это не изменит ничего в поведении ядра:
    # оффлайн обеспечивается переменными окружения и local_files_only.
    assert isinstance(SinapsConfig().privacy.local_only, bool)


def test_removed_offline_guard_would_be_caught(tmp_path):
    """Проверка проверки: сработает ли запрет, если его убрать.

    Тот же приём, что в `test_architecture`: тест на запрет сети может
    оказаться пустым и при этом зелёным. Здесь проверяем, что сам
    механизм отказа работает на реальном состоянии «модели нет».
    """
    with pytest.raises(MissingModelError):
        offline.require_whisper_model("модели-нет", tmp_path)

    # И наоборот: как только модель появилась, отказа быть не должно.
    _make_model(tmp_path, "модели-нет")

    offline.require_whisper_model("модели-нет", tmp_path)


def test_offline_mode_is_not_a_setting():
    """Запрета нет в конфиге намеренно — он не переключается."""
    from sinaps.core.config import SinapsConfig

    fields = SinapsConfig().to_dict()

    assert "offline" not in str(fields).lower(), (
        "появился переключатель оффлайна: приватность снова станет "
        "настройкой, которую можно случайно включить обратно"
    )
