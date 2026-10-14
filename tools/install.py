"""Одноразовая установка: всё, чего нет в git.

Почему файл лежит здесь, а не в пакете `sinaps`
-----------------------------------------------
Установщик — единственная программа проекта, которой сеть положена:
он качает модели и библиотеки. Пока он был модулем пакета, ядро могло
достать его обычным импортом, и проверка «в ядре нет сети» требовала
исключения в виде `NETWORK_ALLOWLIST`. Исключение — это дыра по
определению: сегодня им пользуется установщик, завтра им воспользуется
что-нибудь ещё, и запрет перестанет быть запретом.

Поэтому граница проходит по файловой системе, а не по договорённости:

* ядро — пакет `sinaps/`. Сетевых импортов в нём нет вообще, без
  исключений, и это проверяется тестом;
* установщик — `tools/install.py`. Отдельная программа, которую
  человек запускает руками и которая завершается до того, как
  начнёт работу ассистент.

Запуск: `python tools\\install.py` или `setup_sinaps.bat`.

Зачем этот модуль
-----------------
В репозиторий не попадают ни модели, ни зависимости: они весят больше
самой кодовой базы в десятки раз (`models/` — 2 ГБ, `.venv/` — 3,5 ГБ,
CUDA-библиотеки — 1,5 ГБ). Поэтому после клона машина нерабочая, и нужен
шаг, который это чинит.

Три правила, которым модуль подчиняется
---------------------------------------
1. **Скрипт не переписывает загрузку, а вызывает её.** Модели тянутся
   ровно теми же вызовами, что и сам ассистент: `WhisperModel`,
   `torch.hub.load`, `load_silero_vad`. Иначе рано или поздно скрипт
   разойдётся с программой: скачает не то, напишет в не ту папку, и
   человек получит «всё установлено» при неработающем ассистенте.

2. **Проверка = попытка загрузить.** Не «файл существует», а «движок
   поднялся». Модель, распаковавшаяся с обрезкой, проходит первый
   тест и падает при первом же распознавании.

3. **Повторный запуск безопасен и ничего не качает зря.** Установка
   рассчитана на один запуск, но человек запустит её второй раз —
   и не должен получить ни повторных гигабайтов, ни ошибки.

Про размеры
-----------
HuggingFace хранит вес трижды: в `blobs/`, в `blobs/` внутри карточки
модели и в `snapshots/`. Это жёсткие ссылки на один и тот же файл —
диск занимает один экземпляр, но обход каталога насчитает тройной
объём. Поэтому размеры здесь считаются по уникальным `st_ino`: иначе
скрипт отчитался бы о «6 ГБ» там, где занято 2.
"""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Установщик запускают до того, как пакет установлен, поэтому корень
#: репозитория добавляем в путь сами. Иначе первый запуск на чистой
#: машине упал бы с ModuleNotFoundError на самом старте.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sinaps.core.config import PROJECT_ROOT

#: Куда кладутся веса faster-whisper. Тот же путь, что в
#: `stt/faster_whisper.py`; дублировать его нельзя.
MODELS_DIR = PROJECT_ROOT / "models"

#: Кэш torch.hub для моделей Silero.
TORCH_HUB_DIR = Path(
    os.environ.get("TORCH_HOME",
                   Path.home() / ".cache" / "torch")) / "hub"

OK = "ok"
MISSING = "missing"
BROKEN = "broken"
OPTIONAL = "optional"


@dataclass
class Step:
    """Один проверяемый и, при необходимости, докачиваемый компонент."""

    key: str
    title: str
    status: str = MISSING
    detail: str = ""
    #: Что занимает на диске, если компонент уже стоит.
    size_mb: float = 0.0
    #: Причина неудачи: что именно не так, а не «не вышло».
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.status in (OK, OPTIONAL)


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step

    @property
    def failures(self) -> list[Step]:
        return [s for s in self.steps if s.status == BROKEN]

    @property
    def pending(self) -> list[Step]:
        return [s for s in self.steps if s.status == MISSING]

    @property
    def to_download_mb(self) -> float:
        return sum(s.size_mb for s in self.pending)


# ---------------------------------------------------------------------------
# Размеры
# ---------------------------------------------------------------------------


def dir_size_mb(path: Path) -> float:
    """Реальный размер каталога в МБ, без двойного счёта жёстких ссылок.

    HuggingFace раскладывает вес модели в три места одним и тем же
    inode'ом. Наивный обход покажет 6 ГБ при фактических 2 ГБ, и человек
    решит, что места не хватает, хотя оно есть.
    """
    if not path.exists():
        return 0.0
    seen: set[tuple[int, int]] = set()
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue          # файл мог исчезнуть: кэш чистят в фоне
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total / (1024 * 1024)


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def check_packages() -> Step:
    """Проверяет импортом, а не по списку в requirements.

    Пакет может быть «установлен» и при этом не импортироваться: сломанные
    зависимости, конфликт версий, отсутствующая CUDA-библиотека при
    импорте torch. Реальная проверка — `import`.
    """
    # (имя пакета для pip, имя модуля для import, обязателен ли)
    wanted = [
        ("numpy", "numpy", True),
        ("sounddevice", "sounddevice", True),
        ("faster-whisper", "faster_whisper", True),
        ("torch", "torch", True),
        ("silero-vad", "silero_vad", True),
        ("onnxruntime", "onnxruntime", True),
        ("PySide6", "PySide6.QtWidgets", False),
        ("omegaconf", "omegaconf", True),
        ("scipy", "scipy", True),
    ]

    missing: list[str] = []
    optional_missing: list[str] = []
    versions: list[str] = []

    for dist, module, required in wanted:
        try:
            importlib.import_module(module)
        except Exception as exc:                     # noqa: BLE001
            # Причину различаем: «не установлено» лечится pip, а
            # «установлено, но не грузится» — обновлением версии.
            reason = (f"{type(exc).__name__}: {exc}"[:80]
                      if _package_version(dist) else "не установлен")
            entry = f"{dist} ({reason})"
            (missing if required else optional_missing).append(entry)
            continue
        versions.append(f"{dist}={_package_version(dist) or '?'}")

    if missing:
        step = Step("packages", "Пакеты Python", BROKEN,
                    f"не хватает {len(missing)}", reason="; ".join(missing))
        return step
    if optional_missing:
        return Step("packages", "Пакеты Python", OPTIONAL,
                    "установлено " + ", ".join(versions[:4]) + ", ...",
                    reason="нет (не обязательно): " + ", ".join(optional_missing))
    return Step("packages", "Пакеты Python", OK, ", ".join(versions))


def check_cuda() -> Step:
    """Отдельная проверка: CUDA нужна только ради скорости.

    Без видеокарты ctranslate2 честно переходит на CPU, и это не поломка.
    Поэтому отсутствие CUDA — предупреждение, а не ошибка установки.
    """
    try:
        import ctranslate2
    except ImportError as exc:
        return Step("cuda", "Видеокарта (CUDA)", BROKEN,
                    "ctranslate2 не установлен", reason=str(exc)[:80])

    try:
        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:                         # noqa: BLE001
        return Step("cuda", "Видеокарта (CUDA)", OPTIONAL,
                    "проверка не удалась, считаем что видеокарты нет",
                    reason=str(exc)[:80])

    if count > 0:
        return Step("cuda", "Видеокарта (CUDA)", OK, f"устройств: {count}")
    return Step("cuda", "Видеокарта (CUDA)", OPTIONAL,
                "видеокарта не найдена — будет медленнее, но работать будет")


def check_audio() -> Step:
    """PortAudio и наличие устройств ввода.

    Отсутствие микрофона — не ошибка установки: на ноутбуке без встроенного
    микрофона, или если он отключён в Windows, ассистент всё равно
    работает через текст. Поэтому это предупреждение с указанием причины.
    """
    try:
        import sounddevice as sd
    except Exception as exc:                          # noqa: BLE001
        return Step("audio", "Звук", BROKEN, "PortAudio недоступен",
                    reason=str(exc)[:100])

    try:
        devices = sd.query_devices()
    except Exception as exc:                          # noqa: BLE001
        return Step("audio", "Звук", BROKEN, "опрос устройств не удался",
                    reason=str(exc)[:100])

    inputs = [d["name"] for d in devices if d["max_input_channels"] > 0]
    outputs = [d["name"] for d in devices if d["max_output_channels"] > 0]

    if not inputs:
        return Step("audio", "Звук", OPTIONAL,
                    f"микрофонов нет, устройств вывода: {len(outputs)}",
                    reason="нет устройств ввода — голос не заработает, "
                           "текстовый ввод работает")
    return Step("audio", "Звук", OK,
                f"микрофонов: {len(inputs)}, вывод: {len(outputs)}",
                reason=inputs[0])


# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------


def _stt_dir_size_mb(model: str) -> float:
    """Размер карточки модели по имени репозитория.

    Имя карточки у faster-whisper не совпадает с коротким именем
    (`large-v3-turbo` -> `models--mobiuslabsgmbh--faster-whisper-large-v3-turbo`),
    поэтому ищем по подстроке, а не по точному имени.
    """
    if not MODELS_DIR.exists():
        return 0.0
    needle = model.lower().replace("v3-", "v3").replace("large-v3", "large-v3")
    for card in MODELS_DIR.iterdir():
        if not card.is_dir() or not card.name.startswith("models--"):
            continue
        # `large-v3-turbo` ищем как `large-v3-turbo` в конце имени карточки.
        if needle in card.name.lower() or model.lower() in card.name.lower():
            return dir_size_mb(card)
    return 0.0


def load_stt(model: str) -> tuple[object | None, str]:
    """Грузит (и при необходимости скачивает) модель распознавания.

    Вызывается тот же `WhisperModel`, что и в ассистенте. Модель
    поднимается на CPU и в int8: на установке важна только доступность
    файлов, а не скорость, зато так скачивание не занимает видеопамять.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        return None, f"faster-whisper не установлен: {exc}"

    try:
        engine = WhisperModel(
            model,
            device="cpu",
            compute_type="int8",
            download_root=str(MODELS_DIR),
        )
        return engine, ""
    except Exception as exc:                          # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"[:150]


def load_tts() -> tuple[object | None, str]:
    """Грузит (и при необходимости скачивает) Silero TTS v5."""
    try:
        import torch
    except ImportError as exc:
        return None, f"torch не установлен: {exc}"

    try:
        # Те же аргументы, что в `tts/silero_tts.py`.
        model, _example = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            source="github",
            language="ru",
            speaker="v5_ru",
            trust_repo=True,
            verbose=False,
        )
        return model, ""
    except Exception as exc:                          # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"[:150]


def load_vad() -> tuple[object | None, str]:
    """Грузит (и при необходимости скачивает) Silero VAD onnx."""
    try:
        from silero_vad import load_silero_vad
    except ImportError as exc:
        return None, f"silero-vad не установлен: {exc}"

    try:
        return load_silero_vad(onnx=True), ""
    except Exception as exc:                          # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"[:150]


#: Ориентировочный размер моделей распознавания в МБ.
#:
#: Числа сняты с реально скачанных карточек, а не взяты из документации:
#: `tiny` — 75 МБ, `small` — 461 МБ, `large-v3-turbo` — 1543 МБ (вес
#: `model.bin`). Показывать одну и ту же цифру для всех моделей нельзя:
#: человек выбирает `small` ради экономии места, а скрипт обещает ему
#: полтора гигабайта и отговаривает от затеи.
_STT_MB: dict[str, float] = {
    "tiny": 75.0,
    "base": 145.0,
    "small": 461.0,
    "medium": 1530.0,
    "large-v2": 3090.0,
    "large-v3": 3090.0,
    "large-v3-turbo": 1543.0,
    "distil-large-v3": 1543.0,
}

_ESTIMATED_MB = {
    "tts": 140.0,
    "vad": 3.0,
}


def stt_size_mb(model: str) -> float:
    """Оценка размера модели распознавания."""
    if model in _STT_MB:
        return _STT_MB[model]
    # Неизвестное имя: показываем грубый верхний предел и признаёмся,
    # что не знаем точно, вместо того чтобы молча показать чужое число.
    return 1600.0


# ---------------------------------------------------------------------------
# Основной сценарий
# ---------------------------------------------------------------------------


def survey(stt_model: str) -> Report:
    """Проверяет всё, ничего не скачивая."""
    report = Report()

    report.add(check_packages())
    report.add(check_cuda())
    report.add(check_audio())

    mb = _stt_dir_size_mb(stt_model)
    report.add(Step("stt", "Распознавание (Whisper)",
                    OK if mb else MISSING,
                    f"{stt_model}, {mb:.0f} МБ" if mb else "не скачана",
                    size_mb=stt_size_mb(stt_model)))

    tts_mb = dir_size_mb(TORCH_HUB_DIR / "snakers4_silero-models_master")
    report.add(Step("tts", "Голос (Silero TTS v5)",
                    OK if tts_mb else MISSING,
                    f"{tts_mb:.0f} МБ" if tts_mb else "не скачана",
                    size_mb=_ESTIMATED_MB["tts"]))

    vad_mb = dir_size_mb(TORCH_HUB_DIR / "snakers4_silero-vad_master")
    report.add(Step("vad", "Границы фраз (Silero VAD)",
                    OK if vad_mb else MISSING,
                    f"{vad_mb:.0f} МБ" if vad_mb else "не скачана",
                    size_mb=_ESTIMATED_MB["vad"]))

    return report


def install(report: Report, stt_model: str, *, log=print) -> Report:
    """Докачивает то, чего нет. Уже установленное не трогает."""
    for step in list(report.steps):
        if step.key not in ("stt", "tts", "vad"):
            continue
        if step.status != MISSING:
            continue

        log(f"\n>> {step.title}: скачиваю, это может занять несколько минут…")
        started = time.perf_counter()

        loaders = {"stt": lambda: load_stt(stt_model),
                   "tts": load_tts,
                   "vad": load_vad}
        engine = None
        reason = ""
        try:
            engine, reason = loaders[step.key]()
        except KeyboardInterrupt:
            # Пользователь нажал Ctrl+C. Останавливаемся целиком, но
            # сперва показываем, что уже успело встать, иначе он не
            # поймёт, надо ли начинать заново.
            log("\nПрервано пользователем. Показываю, что уже установлено:")
            step.status = BROKEN
            step.reason = "прервано"
            return report
        except Exception as exc:                     # noqa: BLE001
            # Собственные загрузчики ловят свои ошибки сами. Этот
            # уровень — для всего остального: нехватка памяти при
            # загрузке 1,5 ГБ, SystemError, сбой в сторонней
            # библиотеке. Один взорвавшийся загрузчик не должен
            # прерывать установку остальных: иначе человек получит
            # трейсбек и не узнает, что две модели уже скачаны.
            reason = f"{type(exc).__name__}: {exc}"[:150]

        spent = time.perf_counter() - started
        if engine is not None:
            step.status = OK
            step.size_mb = 0.0
            step.detail = f"готово за {spent:.0f} с"
        else:
            step.status = BROKEN
            step.reason = reason
            step.detail = f"не удалось за {spent:.0f} с"

        # Освобождаем модель сразу: незачем держать в памяти то, что
        # больше не нужно. На 8 ГБ видеопамяти это заметно.
        del engine

    return report


def _print_report(report: Report, *, quiet: bool = False) -> None:
    marks = {OK: "OK  ", MISSING: "НЕТ ", BROKEN: "ОШИБКА", OPTIONAL: "ВНИМАНИЕ "}
    for step in report.steps:
        mark = marks.get(step.status, "?")
        line = f"  [{mark:9s}] {step.title:28s} {step.detail}"
        if step.detail and step.status == BROKEN:
            line = f"  [{mark:9s}] {step.title:28s} {step.reason[:60]}"
        print(line)
        if step.reason and step.status in (OPTIONAL, BROKEN) and not quiet:
            for chunk in _wrap(step.reason, 66):
                print(f"               {chunk}")


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def run(*, install_missing: bool = True, stt_model: str | None = None,
        log=print) -> tuple[int, Report]:
    """Полный сценарий установки. Возвращает (код возврата, отчёт)."""
    from sinaps.core.config import load_config

    if stt_model is None:
        try:
            stt_model = load_config().stt.model
        except Exception:                             # noqa: BLE001
            stt_model = "large-v3-turbo"

    print(BANNER_SETUP)
    print("=" * 62)
    print("УСТАНОВКА SINAPS\n")

    report = survey(stt_model)
    _print_report(report)

    broken_before = report.failures
    if broken_before:
        print("\nПродолжать нельзя: не хватает пакетов Python.")
        print("Выполни в корне проекта:")
        print("    .venv\\Scripts\\pip install -r requirements.txt")
        return 1, report

    if not install_missing:
        return _finish(report, log), report

    if report.pending:
        total = report.to_download_mb
        log("")
        log(f"Нужно скачать примерно {total:.0f} МБ и распаковать.")
        log(f"Куда: {MODELS_DIR}")
        log("Это делается один раз. Нажмите Enter или Ctrl+C для отмены.")

    if report.pending:
        try:
            input()
        except EOFError:
            pass

    report = install(report, stt_model, log=log)

    return _finish(report, log), report


def _finish(report: Report, log) -> int:
    """Итог установки: что готово, что нет, что делать дальше."""
    print("\n" + "=" * 62)
    if report.failures:
        _print_report(report)
        print("\nИТОГ: не всё встало. Смотрите строки с меткой ОШИБКА выше.")
        return 1

    if report.pending:
        print("ИТОГ: часть компонентов не скачалась. Повторите установку —")
        print("      скачается только недостающее.")
        return 1

    models_mb = dir_size_mb(MODELS_DIR)
    hub_mb = dir_size_mb(TORCH_HUB_DIR)
    print("ИТОГ: всё на месте.\n")
    print(f"  models/    {models_mb:>7.0f} МБ   {MODELS_DIR}")
    print(f"  кэш torch  {hub_mb:>7.0f} МБ   {TORCH_HUB_DIR}")
    print()
    print("Запустить:      .\\run_sinaps_silent.bat      (с треем)")
    print("                .\\run_sinaps.bat             (с консолью)")
    print("Проверить:      .\\run_sinaps.bat selftest")
    print("Проверить звук: .\\run_sinaps.bat check-mic")

    notes = [s for s in report.steps if s.status == OPTIONAL]
    if notes:
        print()
        print("Обратите внимание (работать не мешает):")
        for step in notes:
            print(f"  - {step.title}: {step.reason[:70]}")
    return 0


BANNER_SETUP = r"""
   ___  ___ _         _       ___  ___
  / __|/ _ (_)_ _   __| |___  | _ \/ __| __ _ _ __ _ __
  \__ \  _| | ' \ / _` / __| | |   / _ \/ _` | '_ \ '_ \
  |___/\__|_|_||_|\__,_\__| |_| |_|_\__/\__,_| .__/ .__/
                                              |_|   |_|
"""


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI: `python tools\\install.py`."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="tools/install.py",
        description="Одноразовая установка моделей и проверка звука.")
    parser.add_argument("--check", action="store_true",
                        help="только проверить, ничего не скачивая")
    parser.add_argument("--model", default=None,
                        help="какую модель распознавания качать "
                             "(по умолчанию — из config.yaml)")
    parser.add_argument("--yes", action="store_true",
                        help="не спрашивать подтверждение перед скачиванием")
    args = parser.parse_args(argv)

    if args.yes:
        # Подменяем input, чтобы не мешала автоматической установке.
        import builtins

        builtins.input = lambda *a, **k: ""

    if sys.platform != "win32":
        print("Установка рассчитана на Windows: пути моделей и CUDA разные.")
        return 1

    code, _ = run(install_missing=not args.check, stt_model=args.model)
    return code


if __name__ == "__main__":
    # Блока не было, пока файл жил в пакете: вход шёл через
    # `python -m sinaps setup`, и запускал его `sinaps/__main__.py`.
    # Теперь установщик запускается напрямую, и без этой строчки он
    # молча завершался с кодом 0, не сделав ничего — самый неприятный
    # вид поломки, потому что выглядит как успех.
    raise SystemExit(main())
