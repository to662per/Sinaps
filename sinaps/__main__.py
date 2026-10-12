"""Точка входа Sinaps.

    python -m sinaps                  обычный запуск (микрофон + консоль)
    python -m sinaps --selftest       проверка окружения без микрофона
    python -m sinaps --devices        список аудиоустройств
    python -m sinaps --check-mic      проверить, что микрофон слышно
    python -m sinaps --speak "текст"  только синтез, без микрофона
    python -m sinaps --record f.wav   записать 5 секунд (для тестов)
    python -m sinaps --listen         один раз послушать фразу с микрофона
    python -m sinaps --metrics        показать задержки
    python -m sinaps --text-only      без микрофона, только текстовый ввод

Диагностические команды сделаны первыми и как можно проще: если что-то
не работает, сначала `--selftest`, потом `--check-mic`, потом `--listen`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

import numpy as np
from pathlib import Path

from .audio.capture import AudioCapture, dbfs, read_wav, record_to_file, resample
from .audio.devices import has_input_device, print_devices
from .audio.source import FileAudioSource
from .conversation.listening import ListeningPipeline
from .conversation.pipeline import ConversationPipeline, Reply
from .core.bus import EventBus, EventType
from .core.config import load_config, reload_config
from .core.logging_setup import setup_logging
from .core.metrics import Metrics
from .core.session import SessionController
from .core.singleton import SingleInstance, list_instance_note, stop_stale_pid_file
from .runtime import build_runtime, build_skills_runtime, build_stt
from .skills.engine import SkillEngine, build_default_registry
from .skills.registry import SkillRegistry
from .ui.console import HELP, Console, ConsoleCommandRouter

log = logging.getLogger("sinaps")

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

BANNER = r"""
   ___  _                 _
  / _ \(_)_ __  _ __   __| | __ _ _ __
 / ___ \ | '_ \| '_ \ / _` |/ _` | '_ \
/_/   \_\_| .__/| .__/ \__,_|\__,_|_| |_|
         |_|   |_|                      v0.1
"""


# --- служебное --------------------------------------------------------------


def _setup_logging(args: argparse.Namespace) -> None:
    setup_logging()
    root = logging.getLogger()
    if args.verbose:
        root.setLevel(logging.DEBUG)
        logging.getLogger("sinaps").setLevel(logging.DEBUG)
    elif args.quiet:
        logging.getLogger("sinaps").setLevel(logging.WARNING)


# --- диагностические команды ------------------------------------------------


async def cmd_commands(args: argparse.Namespace) -> int:
    """Показывает или сохраняет справочник команд.

    Список генерируется из реестра навыков, поэтому он не может разойтись
    с реальными возможностями: забыть дописать команду невозможно.
    """
    registry = build_default_registry()

    if getattr(args, "out", None):
        return _write_reference(registry, args.out,
                                with_patterns=not getattr(args, "no_patterns", False))

    print(f"Sinaps понимает {len(registry)} команд "
          f"в {len(registry.skills)} навыках\n")
    print(registry.help_text())
    return 0


def _write_reference(registry, path: str, *, with_patterns: bool = True) -> int:
    """Сохраняет справочник в файл.

    UTF-8 с BOM: иначе «Блокнот» в Windows记事те откроется кракозябрами.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = registry.reference_text(with_patterns=with_patterns)

    with open(target, "w", encoding="utf-8-sig", newline="\r\n") as file:
        file.write(text + "\n")

    print(f"Справочник сохранён: {target}")
    print(f"  команд: {len(registry)}")
    print(f"  размер:  {target.stat().st_size / 1024:.1f} КБ")
    return 0


async def cmd_try(args: argparse.Namespace) -> int:
    """Прогоняет фразы через понимание, ничего не выполняя.

    Нужен для отладки NLU: видно, какое намерение выбрано и какие слоты
    извлечены, — без риска что-то реально сделать.
    """
    from .nlu.normalize import normalize
    from .platform.detect import build_platform

    registry = build_default_registry()
    build_platform()  # прогреваем список программ

    phrases = args.text or [
        "который час",
        "какая сегодня дата",
        "открой блокнот",
        "сделай громче",
        "прибавь громкость на пять",
        "выключи звук",
        "статус системы",
        "сколько памяти",
        "выключи компьютер",
        "сделай скриншот",
        "открой файл отчёт",
        "что ты умеешь",
        "сделай абстрактную фигню",
    ]

    print(f"{'фраза':<28} {'намерение':<22} слоты")
    print("-" * 80)
    for phrase in phrases:
        match = registry.match(normalize(phrase))
        if match is None:
            print(f"{phrase:<28} {'-- не найдено --':<22}")
            continue
        slots = ", ".join(f"{k}={_short(v)}" for k, v in match.slots.items()) or "-"
        flag = " [опасное]" if match.spec.dangerous else ""
        print(f"{phrase:<28} {match.spec.name:<22} {slots}{flag}")
    return 0


def _short(value: object) -> str:
    text = str(value)
    return text if len(text) <= 20 else text[:17] + "..."


async def cmd_setup(args: argparse.Namespace) -> int:
    """Одноразовая установка моделей. Не требует работающего ассистента."""
    from .setup import main as setup_main

    argv: list[str] = []
    if getattr(args, "check", False):
        argv.append("--check")
    if getattr(args, "model", None):
        argv += ["--model", args.model]
    if getattr(args, "yes", False):
        argv.append("--yes")
    return setup_main(argv)


async def cmd_selftest(_args: argparse.Namespace) -> int:
    """Проверяет окружение: конфиг, аудио, движки. Без обращения к микрофону."""
    print(BANNER)
    print("=" * 62)
    print("SELF-ТЕСТ SINAPS\n")

    cfg = load_config()
    ok = True

    # 1. Конфигурация
    print("1. Конфигурация")
    try:
        for problem in cfg.validate():
            print(f"   ! предупреждение: {problem}")
        print(f"   OK: STT={cfg.stt.engine}/{cfg.stt.model}, TTS={cfg.tts.engine}, "
              f"VAD={cfg.vad.engine}")
        print(f"   OK: приватность — {'локально, без сети' if cfg.privacy.local_only else 'СЕТЬ РАЗРЕШЕНА!'}")
    except Exception as exc:
        print(f"   FAIL: конфиг: {exc}")
        ok = False

    # 2. Аудиоустройства
    print("\n2. Аудиоустройства")
    try:
        import sounddevice as sd

        inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        outputs = [d for d in sd.query_devices() if d["max_output_channels"] > 0]

        if outputs:
            print(f"   OK: устройств вывода: {len(outputs)}")
        else:
            print("   WARN: устройств вывода не найдено — ответы не будут озвучены")

        if inputs:
            print(f"   OK: микрофонов: {len(inputs)}")
            for dev in sd.query_devices():
                if dev["max_input_channels"] > 0:
                    print(f"      - {dev['name']}")
        else:
            # Не FAIL: голосовой ввод невозможен, но текстовый работает.
            print("   WARN: микрофон не найден.")
            print("          Голосовой ввод работать не будет, но ассистент")
            print("          полностью доступен через текстовый ввод.")
            print("          Проверь: подключён ли микрофон, включён ли он в")
            print("          настройках Windows, не захвачен ли другой программой.")
    except Exception as exc:
        print(f"   FAIL: не удалось опросить аудио: {exc}")
        ok = False

    # 3. Движки
    print("\n3. Модели")
    runtime = None
    try:
        runtime = build_runtime(cfg, with_audio=False)
        started = time.perf_counter()
        runtime.stt.warmup()
        print(f"   OK: STT {runtime.stt.name} загружен за {time.perf_counter() - started:.1f} с "
              f"(устройство: {getattr(runtime.stt, 'device', '?')})")
    except Exception as exc:
        print(f"   FAIL: STT: {exc}")
        ok = False

    try:
        started = time.perf_counter()
        runtime.tts.warmup()
        print(f"   OK: TTS {runtime.tts.name} загружен за {time.perf_counter() - started:.1f} с")
        voices = getattr(runtime.tts, "available_voices", None)
        if voices:
            print(f"   OK: доступные голоса: {', '.join(voices)}")
    except Exception as exc:
        print(f"   FAIL: TTS: {exc}")
        ok = False

    print("\n" + "=" * 62)
    if ok:
        print("ИТОГ: всё в порядке.")
        print("Проверить голосовой ввод:  python -m sinaps check-mic")
        print("Замереть задержки:          python -m sinaps benchmark")
        return 0
    print("ИТОГ: есть проблемы (см. FAIL выше)")
    return 1


async def cmd_devices(_args: argparse.Namespace) -> int:
    print_devices()
    return 0


async def cmd_check_mic(args: argparse.Namespace) -> int:
    """Измеряет уровень сигнала с микрофона за несколько секунд.

    Заодно считает, что думает VAD. Это важно: «микрофон слышен» и
    «система понимает, что это речь» — разные вещи. Если уровень есть,
    а вероятность речи нулевая, дело в чувствительности или в пороге.
    """
    cfg = load_config()
    print(f"Слушаю микрофон {args.seconds:.0f} с. Говорите что-нибудь...\n")

    capture = AudioCapture(
        sample_rate=cfg.audio.sample_rate,
        frame_ms=cfg.audio.frame_ms,
        device=cfg.audio.input_device,
        latency=cfg.audio.latency,
    )
    try:
        await capture.start()
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1

    from .vad.silero_vad import build_vad

    vad = build_vad(cfg.vad.engine, threshold=cfg.vad.threshold)

    levels: list[float] = []
    probs: list[float] = []
    start = time.perf_counter()
    try:
        async for frame in capture.stream():
            probs.append(vad.probability(frame))
            levels.append(dbfs(frame))
            elapsed = time.perf_counter() - start
            level = levels[-1]
            speech = "РЕЧЬ" if probs[-1] >= cfg.vad.threshold else "     "
            bar = "#" * max(0, min(30, int((level + 60) / 2)))
            print(f"\r  {elapsed:4.1f}с {level:6.1f} дБ {speech} |{bar:<30}", end="",
                  flush=True)
            if elapsed >= args.seconds:
                break
    finally:
        await capture.stop()

    print()
    if not levels:
        print("FAIL: не получилось прочитать ни одного кадра")
        return 1

    stats = capture.stats()
    peak = max(levels)
    floor = sorted(levels)[len(levels) // 2]
    speech_share = 100.0 * sum(1 for p in probs if p >= cfg.vad.threshold) / max(1, len(probs))

    # Динамический диапазон. Речь даёт всплески на 15-25 дБ выше фона.
    # Берём 90-й и 10-й перцентили: единичные щелчки не должны давать
    # ложный «разброс», а постоянный гул — ложную речь.
    spread = None
    if len(levels) > 20:
        ordered = sorted(levels)
        high = ordered[int(0.90 * (len(ordered) - 1))]
        low = ordered[int(0.10 * (len(ordered) - 1))]
        spread = high - low

    print(f"\n  Пик: {peak:.1f} дБ, фон: {floor:.1f} дБ")
    if spread is not None:
        print(f"  Разброс громкости: {spread:.1f} дБ "
              f"(у речи бывает 15-25 дБ)")
    print(f"  Записано: {stats['captured_s']} с, потеряно: {stats['dropped_s']} с "
          f"({stats['drop_pct']}%)")
    print(f"  VAD ({cfg.vad.engine}, порог {cfg.vad.threshold}): "
          f"речь {speech_share:.0f}% времени")

    if stats["drop_pct"] > 1.0:
        print(f"  FAIL: потеряно {stats['drop_pct']}% кадров — звук рвётся.")
        print("        Причины: система не успевает (проверь загрузку CPU),")
        print("        или буфер Portaudio мал. Попробуй audio.frame_ms: 40.")
        return 1

    if peak < -55:
        print("  FAIL: сигнал слишком тихий. Проверь микрофон, громкость и вход.")
        return 1

    # Громко есть, но структуры речи нет. Это не про порог.
    if spread is not None and spread < 8.0:
        print("  FAIL: громкость постоянная — микрофон слышит только шум.")
        print("        Речь даёт всплески на 15-25 дБ выше фона, а тут разброс")
        print(f"        всего {spread:.1f} дБ: значит голос в микрофон не попадает.")
        print()
        print("        Что делать:")
        print("        1. УБАВЬ громкость записи в Windows:")
        print("           Параметры -> Система -> Звук -> Ввод -> Громкость.")
        print("           На 100% усиливается сетевой гул 50 Гц, который и")
        print("           маскирует голос. Поставь 50-70%.")
        print("        2. Поднеси микрофон ко рту: дешёвые USB-микрофоны")
        print("           без экрана слышат комнату, а не человека.")
        print("        3. Проверь, не воткнут ли микрофон в переднюю панель")
        print("           корпуса — там ловится гул от системного блока.")
        return 1

    if speech_share < 2.0:
        print(f"  FAIL: сигнал есть ({peak:.0f} дБ), но VAD не признаёт его речью.")
        print(f"        Порог {cfg.vad.threshold} слишком высок. Понизь в config.yaml:")
        print(f"            vad.threshold: 0.3")
        print(f"        Если не поможет — поднеси микрофон ближе или подними")
        print(f"        громкость записи в настройках Windows.")
        return 1

    if peak > -1:
        print("  WARN: сигнал на пределе, будет искажение. Убавь громкость записи.")
    print(f"  OK: микрофон работает, речь распознаётся VAD ({speech_share:.0f}%).")
    return 0


async def cmd_speak(args: argparse.Namespace) -> int:
    """Только синтез: озвучивает текст, микрофон не трогает."""
    cfg = load_config()
    runtime = build_runtime(cfg, with_audio=False)

    print(f"Движок: {runtime.tts.name}, голос {getattr(runtime.tts, 'voice', '-')}")
    started = time.perf_counter()
    pcm = await runtime.tts.synthesize(args.text)
    synth_time = time.perf_counter() - started

    if pcm.size == 0:
        print("FAIL: синтез вернул пустой звук")
        return 1

    duration = len(pcm) / runtime.tts.sample_rate
    print(f"Синтез: {synth_time:.2f} с, звук {duration:.2f} с "
          f"({runtime.tts.sample_rate} Гц), в реальном времени x{ synth_time / duration:.2f}")

    if args.no_play:
        return 0

    played = await runtime.player.play(pcm, sample_rate=runtime.tts.sample_rate)
    print("OK: озвучено" if played else "FAIL: не удалось воспроизвести")
    return 0 if played else 1


async def cmd_hotkeys(args: argparse.Namespace) -> int:
    """Проверка перехвата: показывает, какие кнопки система отдаёт нам.

    Нужна потому, что боковые кнопки мыши часто съедает софт
    производителя (Logitech G Hub, Razer Synapse, X-Mouse): ассистент
    их не видит, а пользователь не понимает, почему.
    """
    cfg = load_config()
    loop = asyncio.get_running_loop()

    from .ui.trigger import ActivationTrigger, parse_trigger

    activate = []
    for spec in (cfg.wake.mouse_button, cfg.wake.hotkey):
        trigger = parse_trigger(spec)
        if trigger is not None and trigger not in activate:
            activate.append(trigger)

    hold = parse_trigger(cfg.wake.push_to_talk, allow_modifiers_only=True)

    wanted = list(activate) + ([hold] if hold is not None else [])
    if not wanted:
        print("Активация не настроена: заполни wake.mouse_button или wake.hotkey")
        return 1

    print("Что ищем:")
    for item in wanted:
        role = "удержание" if item is hold else "открыть окно"
        print(f"  • {item.describe():32s} [{item.raw:12s}] {role}")
    print()

    # Ключи — это `raw`, как их пишет пользователь в конфиге. Сравнивать
    # надо с ними, а не с `describe()`: там читаемое название.
    seen: set[str] = set()

    def _report(source: str, timeout_s: float | None) -> None:
        """Обработчик активации. Сигнатура обязана быть ровно такой:
        вызывается как (откуда, таймаут) из потока ОС."""
        if source in seen:
            return
        seen.add(source)
        mode = "удержание" if timeout_s == float("inf") else "окно на 8 с"
        print(f"  поймано: {source:12s} ({mode})")

    trigger = ActivationTrigger(
        activate=activate,
        hold=hold,
        on_activate=_report,
        on_deactivate=lambda source: None,
        loop=loop,
        debounce_s=0.0,
    )

    error = trigger.start()
    if error:
        print(f"Перехват не запустился: {error}")
        trigger.stop()
        return 1

    print(f"Нажимай что-нибудь {args.seconds:.0f} с. Ctrl+C — выход.\n")
    try:
        await asyncio.sleep(args.seconds)
    finally:
        trigger.stop()

    print("\nИтог:")
    missing = []
    for item in wanted:
        if item.raw in seen:
            print(f"  [OK]  {item.describe()}")
        else:
            print(f"  [--]  {item.describe()} — не поймана")
            missing.append(item)

    if not missing:
        print("\nВсе настроенные кнопки система отдаёт — можно запускать ассистент.")
        return 0

    # Не поймано ВСЁ — перехват не работает вообще. Это другой случай,
    # чем «мышь съела кнопку», и подсказки тут нужны другие.
    if not seen:
        print()
        print("Не поймано ничего — перехват не работает в этой сессии.")
        print("Частые причины:")
        print("  • Ассистент запущен от администратора, а браузер — нет (или наоборот).")
        print("    Перехват видит только ввод внутри своей сессии; запусти оба одинаково.")
        print("  • Сессия без интерактивного рабочего стола (служба, удалёнка без GUI).")
        print("  • Сработала блокировка всплывающих окон / антивирус.")
        return 1

    print()
    lost_mouse = [m for m in missing if m.kind == "mouse"]
    lost_key = [m for m in missing if m.kind == "key"]

    if lost_mouse:
        print("Кнопки мыши, которых не видно:")
        print("  Их перехватывает софт мыши — Logitech G Hub / Options+, Razer Synapse,")
        print("  X-Mouse, Corsair iCUE. В нём сними назначение на «назад/вперёд».")
        print("  Проверь также Параметры Windows -> Мышь -> Дополнительные параметры:")
        print("  кнопки 4 и 5 должны стоять в режиме «Обычный», а не «Навигация».")
        print("  Если так и не заработало — поставь в config.yaml другую кнопку:")
        print("    mouse3 (средняя), mouse4/mouse5 (поменять местами), или ctrl+space.")

    if lost_key:
        print("Сочетания клавиш, которых не видно:")
        print("  Их перехватывает другая программа — PowerToys, AutoHotkey, Antikbd,")
        print("  игровое ПО. Или сочетание занято системой (Ctrl+Esc открывает «Пуск»).")
        print("  Попробуй f9, ctrl+shift+f9 или просто оставь управление с мыши.")

    return 1


async def cmd_evaluate(args: argparse.Namespace) -> int:
    """Сравнивает модели STT на реальных записях.

    Угадывать размер модели по документации нельзя: на чистых фикстурах
    `small` даёт уверенность 0.53-0.91, а на живом микрофоне с шумом
    качество падает совсем иначе. Единственный честный способ —
    прогнать одну и ту же запись через несколько моделей и сравнить.
    """
    from pathlib import Path as _Path

    folder = _Path(args.dir)
    files = sorted(folder.glob("*.wav"))
    if not files:
        print(f"В каталоге {folder} нет wav-файлов.")
        print("Запиши их сам: включи privacy.record_audio в config.yaml")
        print("и поговори с ассистентом, либо используй команду record.")
        return 1

    cfg = load_config()
    print(f"Записей: {len(files)} из {folder}")
    print(f"Язык: {cfg.stt.language}, устройство: {cfg.stt.device}\n")

    header = f"{'модель':20s} {'точность':>10s} {'уверенность':>12s} {'с/фразу':>9s}"
    print(header)
    print("-" * len(header))

    best = None
    for model in args.models.split(","):
        model = model.strip()
        if not model:
            continue
        for compute in args.compute.split(","):
            compute = compute.strip()
            try:
                engine = build_stt(_config_with(cfg, model=model, compute_type=compute))
                engine.warmup()

                # Записи прогреваем заранее: первый запрос на холодной
                # модели стоит секунды и к качеству отношения не имеет.
                first_audio, first_rate = read_wav(str(files[0]))
                engine.transcribe_sync(np.ascontiguousarray(first_audio, np.float32))

                confidences: list[float] = []
                outputs: list[str] = []
                started = time.perf_counter()
                for path in files:
                    audio, rate = read_wav(str(path))
                    if rate != 16000:
                        audio = resample(audio, int(audio.size * 16000 / rate))
                    result = engine.transcribe_sync(
                        np.ascontiguousarray(audio, dtype=np.float32))
                    confidences.append(result.confidence)
                    outputs.append(result.text)
                elapsed = time.perf_counter() - started
                mean = sum(confidences) / len(confidences)
                print(f"{model + ' ' + compute:20s} {'':10s} {mean:12.3f} "
                      f"{elapsed / len(files):9.2f}")
                if best is None or mean > best[0]:
                    best = (mean, model, compute, outputs)
            except Exception as exc:
                print(f"{model + ' ' + compute:20s}  ОШИБКА: {exc}")

    if best is None:
        return 1

    mean, model, compute, outputs = best
    print(f"\nЛучшая по уверенности: {model} / {compute} (средняя {mean:.3f})")
    print(f"\nЧто она услышала:")
    for path, text in zip(files, outputs):
        print(f"  {path.name:24s} {text!r}")
    print("\nОриентир: уверенность выше 0.5 — обычно верный результат,")
    print("ниже 0.2 — почти наверняка мусор. Сравнивай с тем, что говорил.")
    return 0


def _config_with(cfg, **changes):
    """Копия конфига с изменёнными полями."""
    import copy as _copy

    clone = _copy.deepcopy(cfg)
    for key, value in changes.items():
        setattr(clone.stt, key, value)
    return clone


async def cmd_record(args: argparse.Namespace) -> int:
    cfg = load_config()
    record_to_file(args.output, args.seconds, sample_rate=cfg.audio.sample_rate,
                   device=cfg.audio.input_device)
    return 0


async def cmd_listen(args: argparse.Namespace) -> int:
    """Один цикл: послушать фразу, распознать, озвучить ответ, выйти.

    История сюда не пишется намеренно: это диагностика, а не разговор.
    Прогон тестовой фразы не должен появляться в истории пользователя.
    """
    cfg = load_config()

    from_file = bool(args.file)
    # Для файла активация автоматическая: ждать хоткей перед файлом бессмысленно.
    runtime = build_runtime(
        cfg,
        audio_file=args.file,
        require_activation=not from_file,
    )

    # Event, а не исключение: `wait_for` ждёт awaitable, а не класс ошибки.
    done = asyncio.Event()
    got_response = asyncio.Event()
    tts_finished = asyncio.Event()

    async def _finish(event) -> None:  # noqa: ANN001
        if event.type is EventType.RESPONSE_READY:
            print(f"Синапс> {event.payload.get('text', '')}")
            got_response.set()
            done.set()

    async def _on_tts_done(event) -> None:  # noqa: ANN001
        tts_finished.set()

    async def _transcript(event) -> None:  # noqa: ANN001
        text = event.payload.get("text")
        if text:
            print(f"Вы     > {text}  (уверенность {event.payload.get('confidence', 0):.2f})")
        elif event.payload.get("empty"):
            print("Вы     > (нечего не разобрал)")

    runtime.bus.on(EventType.RESPONSE_READY)(_finish)
    runtime.bus.on(EventType.TRANSCRIPT_FINAL)(_transcript)
    runtime.bus.on(EventType.TTS_FINISHED)(_on_tts_done)

    try:
        await runtime.start()
    except Exception as exc:
        print(f"FAIL: не удалось запустить: {exc}")
        return 1

    print(BANNER)
    if from_file:
        print(f"Обрабатываю файл: {args.file}")
    else:
        print(f"Скажи что-нибудь (до {args.timeout:.0f} с)...")
    t0 = time.perf_counter()
    try:
        await runtime.listener.activate(source="cli", timeout_s=args.timeout)
        await asyncio.wait_for(done.wait(), timeout=args.timeout + 30)
        # Дожидаемся конца озвучки. Иначе asyncio отменит синтез на полуслове,
        # а метрика first_audio просто не успеет записаться.
        try:
            await asyncio.wait_for(tts_finished.wait(), timeout=30)
        except asyncio.TimeoutError:
            print("WARN: озвучка не завершилась за 30 с")
    except asyncio.TimeoutError:
        print("FAIL: не дождался ответа")
        return 1
    finally:
        await runtime.stop()

    print(f"\nГотово за {time.perf_counter() - t0:.2f} с")
    if runtime.metrics.stats("total"):
        print(runtime.metrics.report())
    return 0 if got_response.is_set() else 1


class NullSpeaker:
    """Плеер-заглушка для бенчмарка: считает, но не издаёт звука."""

    def __init__(self) -> None:
        self.is_playing = False
        self.played_bytes = 0

    async def play(self, pcm, *, sample_rate=None) -> bool:  # noqa: ANN001
        self.played_bytes += pcm.size
        self.is_playing = True
        await asyncio.sleep(0)
        self.is_playing = False
        return True

    def cancel(self) -> None:
        self.is_playing = False


async def cmd_metrics(args: argparse.Namespace) -> int:
    """Метрики живут только в текущем процессе, поэтому отдельный запуск
    покажет их пустыми. Полезнее `--benchmark`."""
    runtime = build_runtime(load_config(), with_audio=False)
    print("Метрики накопились только в текущей сессии.")
    print(runtime.metrics.report() if runtime.metrics.stats("total")
          else "  (пока нет данных — выполни `--benchmark`)")
    return 0


async def cmd_benchmark(args: argparse.Namespace) -> int:
    """Прогоняет набор фикстур через полный контур и печатает задержки.

    Важно: модели прогреваются заранее. Первый же запрос по холодной модели
    занимает в разы больше и не отражает реальную задержку ассистента.
    """
    cfg = load_config()
    fixtures = sorted(Path(FIXTURES_DIR).glob("*.wav"))
    if not fixtures:
        print(f"FAIL: нет фикстур в {FIXTURES_DIR}")
        return 1

    runtime = build_runtime(cfg, with_audio=False)
    print("Прогреваю модели (это не входит в замер)...", flush=True)
    runtime.stt.warmup()
    runtime.tts.warmup()
    print("Готово.\n")

    metrics = Metrics()
    results: list[tuple[str, float, str, float]] = []

    for wav in fixtures:
        source = FileAudioSource(str(wav), sample_rate=cfg.audio.sample_rate,
                                 frame_ms=cfg.audio.frame_ms)
        bus = EventBus()
        session = SessionController(bus)

        async def responder(text: str, ctx: dict) -> Reply:
            return Reply(speech=f"Услышал: {text}")

        conversation = ConversationPipeline(
            bus=bus, session=session, tts=runtime.tts, player=NullSpeaker(),
            metrics=metrics, responder=responder, speak_enabled=False,
        )
        conversation.install()

        pipeline = ListeningPipeline(
            capture=source, stt=runtime.stt, vad_engine=cfg.vad.engine,
            session=session, bus=bus, metrics=metrics,
            vad_threshold=cfg.vad.threshold, silence_ms=cfg.vad.silence_ms,
            min_speech_ms=cfg.vad.min_speech_ms, require_activation=False,
        )

        await pipeline.start()
        await pipeline.activate(source="bench", timeout_s=30.0)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if conversation.history:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)  # дождаться записи метрик
        await pipeline.stop()

        heard = conversation.history[0]["content"] if conversation.history else "(не услышал)"
        turn = metrics._turns[-1] if metrics._turns else None
        stt_time = turn.marks.get("stt_done", 0) - turn.marks.get("vad_end", 0) if turn else 0.0
        results.append((wav.stem, stt_time, heard, turn.total if turn else 0.0))

    print(f"{'файл':<18} {'STT, с':>8}  распознано")
    print("-" * 62)
    for name, stt_time, heard, _total in results:
        print(f"{name:<18} {stt_time:>8.2f}  {heard}")

    print()
    print(metrics.report())
    fast = metrics.pass_rate("response", threshold=args.threshold)
    print(f"\nОтвет быстрее {args.threshold:.1f} с: {fast * 100:.0f}% диалогов")
    print("Ответ = от конца фразы до первого звука (то, что чувствует пользователь).")
    return 0


# --- основной режим ---------------------------------------------------------


async def run_interactive(args: argparse.Namespace) -> int:
    """Обычный запуск: микрофон (если включён) + текстовая консоль."""
    cfg = load_config()
    with_audio = not args.text_only

    # Вторая копия делит с первой входной аудиопоток, и микрофон начинает
    # «не работать» без всякой видимой причины. Проверяем до сборки графа.
    if with_audio:
        stop_stale_pid_file()
        guard = SingleInstance()
        other = guard.acquire()
        if other is not None:
            print(list_instance_note(other.pid))
            return 1
    else:
        guard = None

    try:
        return await _run_interactive_locked(args, cfg, with_audio)
    finally:
        if guard is not None:
            guard.release()


async def _run_interactive_locked(
    args: argparse.Namespace, cfg, with_audio: bool
) -> int:
    """Собственно запуск. Отделён, чтобы освобождение экземпляра
    нельзя было забыть: `finally` стоит ровно в одном месте."""
    # Этап 2+: полноценное понимание команд вместо эхо-заглушки.
    runtime = build_skills_runtime(cfg, with_audio=with_audio,
                                   user_name=getattr(cfg, "user_name", "Капитан"))
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    # --- консоль ---
    #
    # ВАЖНО: поток чтения stdin — это отдельный поток, а event loop живёт в
    # главном. Обрабатывать реплику напрямую из потока нельзя:
    # asyncio.create_task() вне своего цикла падает, и процесс зависает.
    # Поэтому единственная точка входа — call_soon_threadsafe.
    def _dispatch(line: str) -> None:
        """Выполняется уже в event loop (вызвано через call_soon_threadsafe)."""
        try:
            _process_line(line)
        except Exception:
            log.exception("Ошибка обработки реплики из консоли")
            console.mark_done()

    def _on_console_text(line: str) -> None:
        """Вызывается из потока консоли. Только планировщик, без логики."""
        if not line:
            return
        # Пока предыдущая реплика в работе, поток консоли не читает новую —
        # иначе вывод рассинхронизируется (см. Console.busy).
        console.mark_busy()
        loop.call_soon_threadsafe(_dispatch, line)

    def _process_line(line: str) -> None:
        log.debug("Консоль: строка %r", line)
        try:
            router = ConsoleCommandRouter({
                "help": lambda _: console.info(HELP),
                "devices": lambda _: print_devices(),
                "config": lambda _: print(cfg.dumps()),
                "metrics": lambda _: console.info(runtime.metrics.report()),
                "history": lambda _: _print_history(console, runtime),
                "reload": lambda _: _reload(console, runtime),
                # ВАЖНО: обработчики — это функции, а не их результаты.
                # `_set_quiet(console, runtime)` без lambda выполнился бы
                # тут же, при построении словаря, то есть на КАЖДОЙ строке.
                # Из-за этого озвучка переключалась сама, без команды /quiet.
                "quiet": lambda _: _set_quiet(console, runtime),
                "quit": lambda _: _request_stop(stop),
            })
            if router.route(line):
                return
            # Не команда — это реплика ассистенту. Подтверждаем приём,
            # чтобы пользователь видел: строка понята, ждём обработку.
            console.echo_assistant("обрабатываю…")
            _release_console_later()
            runtime.conversation.handle_text_input(line)
        finally:
            # Служебная команда консоль не блокирует.
            if line.startswith("/"):
                console.mark_done()

    def _release_console_later(timeout: float = 60.0) -> None:
        """Страховка от зависания консоли.

        Если ответ так и не пришёл (диалог отброшен из-за занятой сессии,
        движок упал, исключение проглочено), консоль всё равно должна
        разблокироваться — иначе ввод встанет навсегда.
        """
        async def _release() -> None:
            await asyncio.sleep(timeout)
            if not console.idle.is_set():
                console.info("Нет ответа ассистента, продолжаю ждать ввода.")
                console.mark_done()

        asyncio.get_running_loop().create_task(_release())

    console = Console(_on_console_text, banner=True)

    # --- подписка на события для вывода ---
    async def on_response(event) -> None:  # noqa: ANN001
        # Показываем речь, а не технический display: «Сейчас 17:34» понятнее
        # пользователю, чем «17:34:17, 09.10.2026». Подробности — в логе.
        console.echo_assistant(event.payload.get("text", ""))
        # Сессия удерживается до конца озвучки, поэтому разблокируем ввод
        # по TTS_FINISHED, а не здесь. Иначе следующая реплика придёт
        # в занятую сессию и будет отброшена.
        if not runtime.conversation.speak_enabled:
            console.mark_done()

    async def on_tts_finished(event) -> None:  # noqa: ANN001
        # Ассистент договорил — можно принимать следующую реплику.
        console.mark_done()

    async def on_transcript(event) -> None:  # noqa: ANN001
        text = event.payload.get("text")
        if text:
            console.echo_user(text)

    async def on_error(event) -> None:  # noqa: ANN001
        console.error(f"ошибка в {event.payload.get('where', '?')}: "
                      f"{event.payload.get('detail', '')}")
        console.mark_done()

    runtime.bus.on(EventType.RESPONSE_READY)(on_response)
    runtime.bus.on(EventType.TTS_FINISHED)(on_tts_finished)
    runtime.bus.on(EventType.TRANSCRIPT_FINAL)(on_transcript)
    runtime.bus.on(EventType.ERROR)(on_error)

    # --- запуск ---
    #
    # Нет микрофона — это не поломка, а другая конфигурация машины. Ассистент
    # остаётся полезным: весь разговор идёт через текст. Раньше здесь был
    # `return 1`, из-за чего скрипт падал и выглядел сломанным.
    if with_audio and not has_input_device():
        console.info("Микрофон не найден — включаю текстовый режим.")
        console.info("Весь разговор будет через консоль, голосовой ввод недоступен.")
        console.info("Проверить микрофон: python -m sinaps check-mic")
        runtime = build_skills_runtime(cfg, with_audio=False,
                                       user_name=getattr(cfg, "user_name", "Капитан"))
        with_audio = False

    try:
        await runtime.start()
    except Exception as exc:
        print(f"Не удалось запустить: {exc}")
        print("Подсказка: попробуй `python -m sinaps selftest`")
        return 1

    print(runtime.describe())
    console.info("Пишите команду в консоль" + (" или говорите." if with_audio else "."))
    if cfg.tts.greet_on_start:
        # Тот же приём, что и в трее: приветствие идёт через конвейер
        # разговора, поэтому использует синтез и прерывание. Отдельный
        # вызов движка в обход конвейра создал бы второй путь козвуку.
        asyncio.get_running_loop().call_later(
            1.0, lambda: runtime.conversation.handle_text_input("привет"))
    if with_audio and runtime.trigger is not None:
        if runtime.trigger_error:
            # Молчащий микрофон хуже явной ошибки: пользователь должен
            # знать, почему ассистент не слышит, и куда смотреть.
            console.error(f"Активация не работает: {runtime.trigger_error}")
            console.info("Пока голос недоступен — пишите команды в консоль.")
        else:
            console.info(f"Активация: {runtime.trigger.describe()}")
    console.start()

    try:
        await stop
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.info("Прервано пользователем")
    finally:
        console.stop()
        await runtime.stop()

    console.info("Sinaps остановлен.")
    if runtime.metrics.stats("total"):
        console.info(runtime.metrics.report())
    return 0


def _print_history(console: Console, runtime) -> None:  # noqa: ANN001
    history = runtime.conversation.history
    if not history:
        console.info("История пуста")
        return
    for entry in history:
        who = "Вы " if entry["role"] == "user" else "Син"
        console.info(f"{who}: {entry['content']}")


def _reload(console: Console, runtime) -> None:  # noqa: ANN001
    runtime.config = reload_config()
    console.info("Конфигурация перечитана (движки перезапусти при следующем старте)")


def _set_quiet(console: Console, runtime) -> None:  # noqa: ANN001
    console.set_muted(not console.muted)
    runtime.conversation.set_speak_enabled(not console.muted)


def _request_stop(stop: asyncio.Future) -> None:
    if not stop.done():
        stop.set_result(None)


# --- разбор аргументов -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sinaps",
        description="Sinaps — локальный голосовой ассистент",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Подробнее: ARCHITECTURE.md, README.md",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    parser.add_argument("-q", "--quiet", action="store_true", help="только ошибки")
    parser.add_argument("--text-only", action="store_true",
                        help="не открывать микрофон, работать только с текстом")
    parser.add_argument("--tray", action="store_true",
                        help="запуск с треем и окнами (без консоли)")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="обычный запуск (по умолчанию)")
    # --- установка (одноразовая) -----------------------------------------
    p = sub.add_parser(
        "setup",
        help="одноразовая установка: скачать модели и проверить звук")
    p.add_argument("--check", action="store_true",
                   help="только проверить, ничего не скачивая")
    p.add_argument("--model", default=None,
                   help="какую модель распознавания качать "
                        "(по умолчанию — из config.yaml)")
    p.add_argument("--yes", action="store_true",
                   help="не спрашивать подтверждение перед скачиванием")

    sub.add_parser("selftest", help="проверка окружения без микрофона")
    sub.add_parser("devices", help="список аудиоустройств")
    sub.add_parser("metrics", help="показать задержки")

    p = sub.add_parser("check-mic", help="проверить уровень микрофона")
    p.add_argument("--seconds", type=float, default=5.0)

    p = sub.add_parser("speak", help="озвучить текст")
    p.add_argument("text")
    p.add_argument("--no-play", action="store_true", help="только синтез, без звука")

    p = sub.add_parser("record", help="записать wav")
    p.add_argument("output", nargs="?", default="data/record.wav")
    p.add_argument("--seconds", type=float, default=5.0)

    p = sub.add_parser("evaluate", help="сравнить модели STT на записях")
    p.add_argument("--dir", default="data/audio", help="каталог с wav-файлами")
    p.add_argument("--models", default="small,medium",
                   help="модели через запятую: tiny,base,small,medium,large-v3-turbo")
    p.add_argument("--compute", default="float16,int8_float16",
                   help="точность через запятую: float16, int8_float16, int8")

    p = sub.add_parser("listen", help="один цикл прослушивания")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--file", help="wav-файл вместо микрофона (для отладки и тестов)")

    p = sub.add_parser("hotkeys", help="проверить клавиши и кнопки мыши")
    p.add_argument("--seconds", type=float, default=10.0)

    p = sub.add_parser("benchmark", help="замерить задержки на фикстурах")
    p.add_argument("--threshold", type=float, default=1.6,
                   help="порог «быстрый ответ», сек (по умолчанию 1.6)")

    p = sub.add_parser("commands", help="список всех команд ассистента")
    p.add_argument("--out", metavar="ФАЙЛ",
                   help="сохранить полный справочник в файл (например docs/КОМАНДЫ.md)")
    p.add_argument("--no-patterns", action="store_true",
                   help="в файле не печатать технические шаблоны распознавания")

    p = sub.add_parser("try", help="проверить понимание фраз (ничего не выполняя)")
    p.add_argument("text", nargs="*", help="фразы для проверки")

    return parser


_COMMANDS = {
    "setup": cmd_setup,
    "selftest": cmd_selftest,
    "devices": cmd_devices,
    "check-mic": cmd_check_mic,
    "speak": cmd_speak,
    "record": cmd_record,
    "listen": cmd_listen,
    "hotkeys": cmd_hotkeys,
    "evaluate": cmd_evaluate,
    "metrics": cmd_metrics,
    "benchmark": cmd_benchmark,
    "commands": cmd_commands,
    "try": cmd_try,
}


def run_tray(args: argparse.Namespace) -> int:
    """Запуск с треем: ассистент живёт в фоне, управление из меню.

    Синхронная: `QtHost` сам поднимает event loop в отдельном потоке,
    и оборачивать это в `asyncio.run()` значило бы создать вторую,
    пустую петлю поверх.
    """
    from .ui.qt_host import start_tray_mode

    # Защита от второй копии нужна и здесь: две копии делят микрофон.
    stop_stale_pid_file()
    guard = SingleInstance()
    other = guard.acquire()
    if other is not None:
        print(list_instance_note(other.pid))
        return 1

    try:
        return start_tray_mode(load_config(), console=args.verbose,
                               no_audio=args.text_only)
    finally:
        guard.release()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Флаги без подкоманды (`python -m sinaps --selftest`).
    if args.command is None:
        for name in _COMMANDS:
            if getattr(args, name.replace("-", "_"), False):
                args.command = name
                break

    _setup_logging(args)
    command = getattr(args, "command", None)

    try:
        if getattr(args, "tray", False):
            return run_tray(args) or 0
        if command and command in _COMMANDS:
            return asyncio.run(_COMMANDS[command](args)) or 0
        return asyncio.run(run_interactive(args)) or 0
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
