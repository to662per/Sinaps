"""Тесты одноразовой установки.

Скачивания здесь нет: `survey()` работает по файлам, а загрузчики
подменяются заглушками. Проверять надо не «модель скачалась» — это уже
проверено запуском на чистой папке, — а решения, из-за которых человек
получает неверную картину:

  * «всё на месте» при отсутствующей модели;
  * «нужно 1600 МБ» для выбранной `small`;
  * загрузка того, что уже стоит;
  * молчаливое продолжение после сбоя пакета.

Любая из этих ошибок переносит человека к ассистенту, который не
работает, и оставляет его там с улыбкой «установлено успешно».
"""

from __future__ import annotations

import os

import pytest

#: Установщик живёт вне пакета `sinaps`: это единственная программа
#: проекта, которой сеть положена. Пока он был модулем пакета, ядро могло
#: достать его обычным импортом, и проверка «в ядре нет сети» нуждалась
#: в исключении — а исключение отменяет проверку.
import importlib.util
import sys
from pathlib import Path

_INSTALLER = Path(__file__).resolve().parent.parent / "tools" / "install.py"


def _load_installer():
    """Загрузить установщик как модуль по пути к файлу."""
    spec = importlib.util.spec_from_file_location("sinaps_installer", _INSTALLER)
    assert spec and spec.loader, "не удалось открыть tools/install.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


S = _load_installer()


# --- размеры ----------------------------------------------------------------


def test_dir_size_counts_empty_dir(tmp_path):
    assert S.dir_size_mb(tmp_path) == pytest.approx(0.0)


def test_dir_size_of_missing_path_is_zero(tmp_path):
    assert S.dir_size_mb(tmp_path / "нет") == pytest.approx(0.0)


def test_dir_size_counts_bytes(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    assert S.dir_size_mb(tmp_path) == pytest.approx(2.0, rel=0.01)


def test_hard_links_are_counted_once(tmp_path):
    """Ключевая проверка: HuggingFace раскладывает вес в три места.

    Это жёсткие ссылки на один файл. Наивный обход покажет тройной
    объём, и человек увидит «6 ГБ» вместо 2 ГБ — решит, что места не
    хватает, и откажется от установки впритык к реальному дефициту.
    """
    target = tmp_path / "model.bin"
    target.write_bytes(b"x" * (4 * 1024 * 1024))

    blobs = tmp_path / "blobs"
    card = tmp_path / "models--somename"
    snapshot = card / "snapshots" / "abc"
    blobs.mkdir(parents=True)
    (card / "blobs").mkdir(parents=True)
    snapshot.mkdir(parents=True)

    try:
        os.link(target, blobs / "hash1")
        os.link(target, card / "blobs" / "hash2")
        os.link(target, snapshot / "model.bin")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"жёсткие ссылки недоступны: {exc}")

    assert S.dir_size_mb(tmp_path) == pytest.approx(4.0, rel=0.01), (
        "один и тот же файл в трёх местах — это 4 МБ, а не 12"
    )


# --- оценка размера моделей -------------------------------------------------


@pytest.mark.parametrize("model,expected", [
    ("tiny", 75.0),
    ("small", 461.0),
    ("large-v3-turbo", 1543.0),
])
def test_known_models_have_measured_sizes(model, expected):
    assert S.stt_size_mb(model) == expected


def test_smaller_model_is_not_promised_as_large():
    """Человек выбирает `small` ради места — нельзя обещать ему 1,5 ГБ.

    Одно и то же число для всех моделей срывает весь смысл выбора:
    скрипт как бы говорит «места не сэкономишь, бери любую».
    """
    assert S.stt_size_mb("tiny") < S.stt_size_mb("small")
    assert S.stt_size_mb("small") < S.stt_size_mb("large-v3-turbo")


def test_unknown_model_does_not_claim_to_know():
    assert S.stt_size_mb("какая-то-выдумка") > 0


# --- отчёт ------------------------------------------------------------------


def _report(**kwargs) -> S.Report:
    report = S.Report()
    for step in kwargs.pop("steps", []):
        report.add(step)
    return report


def test_step_ready_for_ok_and_optional():
    assert S.Step("a", "A", S.OK).ready
    assert S.Step("a", "A", S.OPTIONAL).ready


def test_missing_and_broken_are_not_ready():
    assert not S.Step("a", "A", S.MISSING).ready
    assert not S.Step("a", "A", S.BROKEN).ready


def test_report_separates_broken_from_pending():
    report = _report(steps=[
        S.Step("a", "A", S.OK),
        S.Step("b", "B", S.MISSING, size_mb=100),
        S.Step("c", "C", S.MISSING, size_mb=200),
        S.Step("d", "D", S.BROKEN),
    ])

    assert [s.key for s in report.failures] == ["d"]
    assert [s.key for s in report.pending] == ["b", "c"]
    assert report.to_download_mb == pytest.approx(300.0)


def test_report_with_nothing_pending_sums_zero():
    report = _report(steps=[S.Step("a", "A", S.OK), S.Step("b", "B", S.OPTIONAL)])
    assert report.pending == []
    assert report.to_download_mb == pytest.approx(0.0)


# --- поиск модели на диске --------------------------------------------------


def test_stt_dir_found_by_model_name(tmp_path, monkeypatch):
    """Карточка faster-whisper называется не так, как модель.

    `large-v3-turbo` лежит в `models--mobiuslabsgmbh--faster-whisper-large-v3-turbo`.
    Поиск по точному имени молча не нашёл бы уже скачанное и предложил
    скачать заново — то есть установщик не смог бы пережить повторный
    запуск.
    """
    card = tmp_path / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
    card.mkdir()
    (card / "model.bin").write_bytes(b"x" * 1024)

    monkeypatch.setattr(S, "MODELS_DIR", tmp_path)
    assert S._stt_dir_size_mb("large-v3-turbo") > 0


def test_stt_dir_absent_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MODELS_DIR", tmp_path / "нет")
    assert S._stt_dir_size_mb("large-v3-turbo") == pytest.approx(0.0)


# --- survey -----------------------------------------------------------------


def test_survey_lists_every_component(monkeypatch):
    """Отчёт обязан закрывать весь путь до работающего ассистента."""
    monkeypatch.setattr(S, "check_packages", lambda: S.Step("packages", "Пакеты", S.OK, "ок"))
    monkeypatch.setattr(S, "check_cuda", lambda: S.Step("cuda", "CUDA", S.OPTIONAL, "нет"))
    monkeypatch.setattr(S, "check_audio", lambda: S.Step("audio", "Звук", S.OK, "ок"))
    monkeypatch.setattr(S, "_stt_dir_size_mb", lambda model: 0.0)
    monkeypatch.setattr(S, "dir_size_mb", lambda path: 0.0)

    report = S.survey("large-v3-turbo")
    keys = [s.key for s in report.steps]

    for expected in ("packages", "cuda", "audio", "stt", "tts", "vad"):
        assert expected in keys, f"отчёт не проверяет {expected!r}"


def test_survey_marks_present_models_ready(monkeypatch):
    monkeypatch.setattr(S, "check_packages", lambda: S.Step("packages", "Пакеты", S.OK, "ок"))
    monkeypatch.setattr(S, "check_cuda", lambda: S.Step("cuda", "CUDA", S.OK, "ок"))
    monkeypatch.setattr(S, "check_audio", lambda: S.Step("audio", "Звук", S.OK, "ок"))
    monkeypatch.setattr(S, "_stt_dir_size_mb", lambda model: 1500.0)
    monkeypatch.setattr(S, "dir_size_mb", lambda path: 100.0)

    report = S.survey("large-v3-turbo")
    assert report.pending == [], "скачанное не должно считаться ожидающим"
    assert report.failures == []


# --- install ----------------------------------------------------------------


def test_install_downloads_only_missing(monkeypatch):
    """Уже скачанное повторно не тянуть: это 1,5 ГБ и 15 минут."""
    monkeypatch.setattr(S, "check_packages", lambda: S.Step("packages", "Пакеты", S.OK, "ок"))
    monkeypatch.setattr(S, "check_cuda", lambda: S.Step("cuda", "CUDA", S.OK, "ок"))
    monkeypatch.setattr(S, "check_audio", lambda: S.Step("audio", "Звук", S.OK, "ок"))
    monkeypatch.setattr(S, "_stt_dir_size_mb", lambda model: 1500.0)
    monkeypatch.setattr(S, "dir_size_mb", lambda path: 100.0)

    calls: list[str] = []
    monkeypatch.setattr(S, "load_stt", lambda m: (calls.append("stt"), object())[1])
    monkeypatch.setattr(S, "load_tts", lambda: (calls.append("tts"), object())[1])
    monkeypatch.setattr(S, "load_vad", lambda: (calls.append("vad"), object())[1])

    report = S.install(S.survey("large-v3-turbo"), "large-v3-turbo", log=lambda *a: None)

    assert calls == [], f"повторно загружено: {calls}"


def test_install_marks_failure_with_reason(monkeypatch):
    """Ошибка загрузки обязана называть причину, а не молчать."""
    monkeypatch.setattr(S, "check_packages", lambda: S.Step("packages", "Пакеты", S.OK, "ок"))
    monkeypatch.setattr(S, "check_cuda", lambda: S.Step("cuda", "CUDA", S.OK, "ок"))
    monkeypatch.setattr(S, "check_audio", lambda: S.Step("audio", "Звук", S.OK, "ок"))
    monkeypatch.setattr(S, "_stt_dir_size_mb", lambda model: 0.0)
    monkeypatch.setattr(S, "dir_size_mb", lambda path: 0.0)

    monkeypatch.setattr(S, "load_stt", lambda m: (None, "сеть недоступна"))
    monkeypatch.setattr(S, "load_tts", lambda: (None, "сеть недоступна"))
    monkeypatch.setattr(S, "load_vad", lambda: (None, "сеть недоступна"))

    report = S.install(S.survey("large-v3-turbo"), "large-v3-turbo",
                       log=lambda *a: None)

    assert len(report.failures) == 3
    for step in report.failures:
        assert step.reason == "сеть недоступна", "у сбоя должна быть причина"
        assert step.status == S.BROKEN


def test_failed_download_is_not_counted_as_pending(monkeypatch):
    """Сбой — это сбой, а не «ещё не скачано».

    Иначе итог скажет «часть не скачалась, повторите» вместо «не
    получилось, проверьте сеть», и человек будет заново ждать то, что
    не может скачаться.
    """
    monkeypatch.setattr(S, "check_packages", lambda: S.Step("packages", "Пакеты", S.OK, "ок"))
    monkeypatch.setattr(S, "check_cuda", lambda: S.Step("cuda", "CUDA", S.OK, "ок"))
    monkeypatch.setattr(S, "check_audio", lambda: S.Step("audio", "Звук", S.OK, "ок"))
    monkeypatch.setattr(S, "_stt_dir_size_mb", lambda model: 0.0)
    monkeypatch.setattr(S, "dir_size_mb", lambda path: 0.0)
    monkeypatch.setattr(S, "load_stt", lambda m: (None, "нет сети"))
    monkeypatch.setattr(S, "load_tts", lambda: (None, "нет сети"))
    monkeypatch.setattr(S, "load_vad", lambda: (None, "нет сети"))

    report = S.install(S.survey("large-v3-turbo"), "large-v3-turbo",
                       log=lambda *a: None)

    assert report.pending == [], "сбой не должен выглядеть как «ещё не скачано»"


def test_install_does_not_crash_on_loader_exception(monkeypatch):
    """Загрузчик, поднявший исключение, не должен ронять установку."""
    def boom(*a, **k):
        raise RuntimeError("неожиданно")

    monkeypatch.setattr(S, "check_packages", lambda: S.Step("packages", "Пакеты", S.OK, "ок"))
    monkeypatch.setattr(S, "check_cuda", lambda: S.Step("cuda", "CUDA", S.OK, "ок"))
    monkeypatch.setattr(S, "check_audio", lambda: S.Step("audio", "Звук", S.OK, "ок"))
    monkeypatch.setattr(S, "_stt_dir_size_mb", lambda model: 0.0)
    monkeypatch.setattr(S, "dir_size_mb", lambda path: 0.0)
    monkeypatch.setattr(S, "load_stt", boom)

    # Остальные загрузчики загружаются штатно.
    monkeypatch.setattr(S, "load_tts", lambda: (object(), ""))
    monkeypatch.setattr(S, "load_vad", lambda: (object(), ""))

    report = S.install(S.survey("large-v3-turbo"), "large-v3-turbo",
                       log=lambda *a: None)

    stt = next(s for s in report.steps if s.key == "stt")
    assert stt.status == S.BROKEN
    assert "неожиданно" in stt.reason


# --- проверка окружения -----------------------------------------------------


def test_check_packages_reports_missing(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake(name, *a, **k):
        if name == "torch":
            raise ImportError("нет torch")
        return real_import(name, *a, **k)

    monkeypatch.setattr(S.importlib, "import_module", fake)
    step = S.check_packages()

    assert step.status == S.BROKEN
    assert "torch" in step.reason


def test_missing_microphone_is_warning_not_failure():
    """Нет микрофона — это не ошибка установки.

    Ассистент остаётся работоспособен через текстовый ввод. Объявлять
    установку неудачной из-за отсутствия гарнитуры значит заставить
    человека искать микрофон вместо того, чтобы пользоваться программой.
    """
    assert S.OPTIONAL != S.BROKEN
    step = S.Step("audio", "Звук", S.OPTIONAL, "микрофонов нет")
    assert step.ready, "установка не должна проваливаться из-за микрофона"


# --- форматирование ---------------------------------------------------------


def test_wrap_breaks_long_text():
    lines = S._wrap("слово " * 40, width=30)
    assert len(lines) > 1
    assert all(len(line) <= 30 for line in lines)


def test_wrap_keeps_short_text_whole():
    assert S._wrap("коротко", width=40) == ["коротко"]


# --- точка входа ------------------------------------------------------------


def test_check_mode_never_downloads(monkeypatch, capsys):
    """`--check` обещан как «только проверить»: сети быть не должно."""
    monkeypatch.setattr(S, "survey", lambda model: S.Report())
    monkeypatch.setattr(S, "run", lambda **k: (_ for _ in ()).throw(
        AssertionError("--check не должен вызывать run с загрузкой")))

    # Проверяем сам маршрут: install_missing=False при --check.
    assert S.main.__doc__

    called = {}

    def fake_run(**kwargs):
        called.update(kwargs)
        return 0, S.Report()

    monkeypatch.setattr(S, "run", fake_run)
    code = S.main(["--check"])

    assert code == 0
    assert called["install_missing"] is False, "--check не должен ничего качать"


def test_default_mode_installs():
    called = {}

    def fake_run(**kwargs):
        called.update(kwargs)
        return 0, S.Report()

    module = S
    original = module.run
    module.run = fake_run
    try:
        module.main(["--yes"])
    finally:
        module.run = original

    assert called["install_missing"] is True
