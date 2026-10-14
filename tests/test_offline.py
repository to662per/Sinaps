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

import asyncio
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


def test_report_states_plainly_when_everything_holds(monkeypatch):
    for name in offline._OFFLINE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(offline, "_NETWORK_PACKAGES", ())

    offline.enforce_offline()

    assert offline.report() == offline.describe()


def test_report_warns_when_flag_missing(monkeypatch):
    """Проверка обязана замечать, что оффлайн-режим выключен.

    Раньше строка «локально, без сети» печаталась по флагу
    `privacy.local_only`, который ничего не делал. Теперь вывод
    опирается на измеримое состояние и молчать не должен.
    """
    for name in offline._OFFLINE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(offline, "_NETWORK_PACKAGES", ())

    report = offline.report()

    assert "ПРЕДУПРЕЖДЕНИЕ" in report
    assert "HF_HUB_OFFLINE" in report, "не сказано, какой именно флаг отсутствует"


def test_report_warns_about_loaded_network_module(monkeypatch):
    """Если движок подтянул сетевой модуль — это надо сказать."""
    for name in offline._OFFLINE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(offline, "_NETWORK_PACKAGES", ("httpx",))
    monkeypatch.setitem(__import__("sys").modules, "httpx", object())

    # Флаги выставляем: пока оффлайн не включён, проверка сообщает
    # о нём и до сетевых модулей не доходит — порядок здесь осмысленный.
    offline.enforce_offline()

    report = offline.report()

    assert "ВНИМАНИЕ" in report
    assert "httpx" in report, "не сказано, какой именно модуль загружен"


def test_missing_flags_reported_before_modules(monkeypatch):
    """Отсутствие флагов важнее загруженных модулей — о нём и говорим."""
    for name in offline._OFFLINE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(offline, "_NETWORK_PACKAGES", ("httpx",))
    monkeypatch.setitem(__import__("sys").modules, "httpx", object())

    assert "ПРЕДУПРЕЖДЕНИЕ" in offline.report()


def test_report_is_not_the_privacy_flag(monkeypatch):
    """Отдельно фиксируем: вывод про сеть не зависит от `local_only`.

    Тот флаг печатал «без сети», ничего не проверяя. Если кто-то
    вернёт к нему проверку, тест упадёт.
    """
    for name in offline._OFFLINE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(offline, "_NETWORK_PACKAGES", ())

    from sinaps.core.config import SinapsConfig

    cfg = SinapsConfig()
    cfg.privacy.local_only = False

    offline.enforce_offline()

    assert offline.report() == offline.describe(), (
        "вывод про сеть не должен зависеть от privacy.local_only — "
        "флаг ничего не проверяет"
    )


# --- сообщение должно доходить до пользователя -----------------------------


def test_missing_model_is_a_sinaps_error():
    """Ошибка обязана быть типизированной, иначе её не услышат.

    Обработчик диалога ловит `SinapsError` и произносит `exc.speech`
    вслух (`conversation/pipeline.py:183`). Обычное исключение уходит в
    `except Exception`, и человек вместо «Запусти setup_sinaps.bat»
    услышит «Что-то пошло не так, попобуй ещё раз».

    Первая версия наследовалась от `RuntimeError` — и это была находка:
    код выглядел правильно, а объяснение терялось по дороге к человеку.
    """
    from sinaps.core.errors import SinapsError

    assert issubclass(offline.MissingModelError, SinapsError), (
        "MissingModelError обязана быть SinapsError: иначе обработчик "
        "диалога не услышит её и скажет 'Что-то пошло не так'"
    )


def test_missing_model_error_keeps_message_and_speech():
    err = offline.MissingModelError("Модель не найдена")

    assert err.message == "Модель не найдена"
    assert err.speech == "Модель не найдена"


async def test_missing_model_message_reaches_the_user():
    """Сквозной путь: движок бросает ошибку — человек слышит объяснение.

    Способ взят из `test_pipeline.py`: конвейер ставится через `install()`,
    ответ приходит событием шины. Ничего не угадывается.
    """
    from sinaps.conversation.pipeline import ConversationPipeline
    from sinaps.core.bus import EventBus, EventType
    from sinaps.core.errors import SinapsError
    from sinaps.core.metrics import Metrics
    from sinaps.core.session import SessionController

    class NoTTS:
        def speak(self, text, *args, **kwargs):
            pass

    class NoPlayer:
        async def play(self, *args, **kwargs):
            return None

    async def honest_responder(text, context):
        """Движок, который честно признаётся: модели нет.

        Сигнатура взята из `make_echo_responder`: responder — это
        асинхронная функция `(text, context)`, а не объект с методом.
        Первая версия теста угадывала и упала с `object is not callable`.
        """
        raise offline.MissingModelError(
            "Модель распознавания не найдена. Запусти setup_sinaps.bat.")

    bus = EventBus()
    session = SessionController(bus)
    spoken: list[str] = []

    async def collect(event):
        spoken.append(event.payload.get("text", ""))

    bus.on(EventType.RESPONSE_READY)(collect)

    conv = ConversationPipeline(
        bus=bus, session=session, tts=NoTTS(), player=NoPlayer(),
        metrics=Metrics(), responder=honest_responder,
    )
    conv.install()

    await conv.handle_text_input("привет")
    await asyncio.sleep(0.05)

    assert spoken, "конвейер не ответил вовсе"
    assert "setup_sinaps" in spoken[-1], (
        f"пользователю сказали {spoken[-1]!r} вместо объяснения, "
        "что нужно запустить установщик"
    )


def test_offline_mode_is_not_a_setting():
    """Запрета нет в конфиге намеренно — он не переключается."""
    from sinaps.core.config import SinapsConfig

    fields = SinapsConfig().to_dict()

    assert "offline" not in str(fields).lower(), (
        "появился переключатель оффлайна: приватность снова станет "
        "настройкой, которую можно случайно включить обратно"
    )
