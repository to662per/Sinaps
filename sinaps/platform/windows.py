"""Возможности Windows.

Реализация намеренно использует только то, что уже есть в системе:
`ctypes` (Win32 API), `subprocess` (powershell/schtasks) и `psutil`.
Никаких `pywin32` / `pycaw` — это десятки мегабайт ради двух функций.

Что как сделано и почему:
  * Громкость — виртуальные клавиши (VK_VOLUME_*). Работает везде, не
    требует COM-объекта аудиоэндпоинта. Минус: нельзя узнать абсолютную
    громкость и поставить её точно, только +/- и mute.
  * Окна — EnumWindows/GetWindowText. Даёт список открытых окон.
  * Приложения — реестр `App Paths` + ярлыки меню «Пуск». Это то, чем
    пользуется сам Windows при поиске в Пуске.
  * Питание — `shutdown.exe` и `rundll32 powrprof.dll,SetSuspendState`.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from ..core.logging_setup import get_dialogue_logger
from .base import (
    AppInfo,
    AppLaunchMethod,
    PowerAction,
    SystemStatus,
    TextSelection,
    VolumeState,
    WindowInfo,
)

log = logging.getLogger(__name__)

# --- Win32 -------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None

#: Виртуальные клавиши громкости. Работают с любой звуковой подсистемой.
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_KEYUP = 0x0002

#: Сочетание для скриншота.
VK_LCONTROL, VK_LWIN, VK_PRINT = 0xA2, 0x5B, 0x2C

SW_MINIMIZE = 6

#: Порог нечёткого совпадения для ПРЕДЛОЖЕНИЙ пользователю.
#:
#: Замерено на реальном списке программ: верные совпадения дают 0.67–0.86
#: («стим» -> Steam = 0.67), ложные — 0.53–0.67 («chrome» -> Heroes III =
#: 0.67). Классы перекрываются, поэтому автоматически запускать по нечёткому
#: совпадению нельзя: ниже порога мы только предлагаем варианты.
_FUZZY_SUGGEST = 0.70

#: Где Windows держит ярлыки установленных программ.
_START_MENU_DIRS = (
        Path(os.environ.get("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
)

_POWER_COMMANDS = {
    PowerAction.SHUTDOWN: ["shutdown", "/s", "/t", "{delay}"],
    PowerAction.RESTART: ["shutdown", "/r", "/t", "{delay}"],
}


#: Встроенные приложения Windows.
#:
#: Нужны потому, что Блокнот, Калькулятор и Paint — это UWP-приложения: их
#: нет в списке ярлыков меню «Пуск», но лежат в System32 и запускаются по
#: имени. Без этой таблицы «открой блокнот» не сработало бы.
_BUILTIN_APPS = {
    # ключ -> (исполняемый файл, как называть вслух)
    "блокнот": ("notepad.exe", "Блокнот"),
    "notepad": ("notepad.exe", "Блокнот"),
    "текстовый редактор": ("notepad.exe", "Блокнот"),
    "калькулятор": ("calc.exe", "Калькулятор"),
    "calc": ("calc.exe", "Калькулятор"),
    "кальк": ("calc.exe", "Калькулятор"),
    "paint": ("mspaint.exe", "Paint"),
    "пейнт": ("mspaint.exe", "Paint"),
    "проводник": ("explorer.exe", "Проводник"),
    "файловый менеджер": ("explorer.exe", "Проводник"),
    "командная строка": ("cmd.exe", "Командная строка"),
    "cmd": ("cmd.exe", "Командная строка"),
    "терминал": ("wt.exe", "Терминал"),
    "powershell": ("powershell.exe", "PowerShell"),
    "диспетчер задач": ("taskmgr.exe", "Диспетчер задач"),
    "таскменеджер": ("taskmgr.exe", "Диспетчер задач"),
    "экранная клавиатура": ("osk.exe", "Экранная клавиатура"),
    "клавиатура": ("osk.exe", "Экранная клавиатура"),
    "wordpad": ("write.exe", "WordPad"),
    "снимок экрана": ("snippingtool.exe", "Ножницы"),
    "ножницы": ("snippingtool.exe", "Ножницы"),
    "regedit": ("regedit.exe", "Редактор реестра"),
    "редактор реестра": ("regedit.exe", "Редактор реестра"),
    "msconfig": ("msconfig.exe", "Конфигурация системы"),
    "services": ("services.msc", "Службы"),
    "свойства экрана": ("control.exe display", "Свойства экрана"),
    "цвет": ("colorcpl.exe", "Цвета"),
    "персонализация": ("control.exe desktop", "Персонализация"),
    "планировщик задач": ("taskschd.msc", "Планировщик заданий"),
    "mmc": ("mmc.exe", "Консоль управления"),
    "звук": ("sndvol.exe", "Свойства звука"),
    "свойства звука": ("sndvol.exe", "Свойства звука"),
}

#: Таблица транслитерации для брендовых названий: «телеграм» -> telegram.
_RU_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_LAT_TO_RU = {v: k for k, v in _RU_TO_LAT.items() if len(v) <= 2 and v}


def _norm(text: str) -> str:
    """Приводит к сравнимому виду: нижний регистр, без «ё» и лишних пробелов."""
    text = (text or "").lower().replace("ё", "е")
    return " ".join(text.split())


def _ru_to_lat(text: str) -> str:
    return "".join(_RU_TO_LAT.get(ch, ch) for ch in _norm(text))


def _lat_to_ru(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        for length in (2, 1):
            chunk = text[i:i + length]
            if chunk in _LAT_TO_RU:
                out.append(_LAT_TO_RU[chunk])
                i += length
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _variants(text: str) -> set[str]:
    """Набор эквивалентных форм слова: оригинал + транслитерации."""
    base = _norm(text)
    variants = {base}
    for candidate in (_ru_to_lat(base), _lat_to_ru(base)):
        if candidate:
            variants.add(candidate)
    return variants


def _match_quality(query: str, name: str) -> int:
    """Насколько уверенно `query` означает `name`. 0 — не совпадение.

    Ранжирование по границам слов нужно, чтобы «word» не нашёл
    «Password Manager», а «autocad» нашёл «AutoCAD 2026».
    """
    if not query or not name:
        return 0
    if query == name:
        return 100
    # Запрос — начало названия, и после него граница слова.
    if name.startswith(query + " ") or name.startswith(query + "-"):
        return 90
    # Название — начало запроса.
    if query.startswith(name + " "):
        return 80
    # Запрос — отдельное слово внутри названия.
    if f" {query} " in f" {name} ":
        return 70
    return 0


#: Слова короче этого не дают полезных совпадений («7», «и»).
_MIN_TOKEN_LEN = 3

#: Порог нечёткого совпадения для запасного поиска.
#:
#: Подбирался на реальных промахах распознавания: «блокно» против
#: «блокнот» даёт ~0.91, а «кальк» против «калькулятор» — ~0.6.
#: Порог 0.72 пропускает первое и отсекает второе.
FUZZY_MIN_SCORE = 0.72

#: Насколько лучший кандидат должен быть лучше второго. Иначе ассистент
#: угадывает, а человек уверен, что открылось нужное.
FUZZY_MIN_GAP = 0.08


def _name_forms(name: str) -> set[str]:
    """Формы для нечёткого сравнения: варианты названия + варианты его слов.

    Зачем слова: `difflib` штрафует за разницу длин, поэтому сравнение
    «avtokad» с «AutoCAD 2026 - Русская версия» даёт низкий балл. Если
    сравнивать ещё и с отдельным словом «autocad», совпадение находится.
    """
    forms = _variants(name)
    for token in re.split(r"[\s\-_()\[\],.]+", _norm(name)):
        if len(token) >= _MIN_TOKEN_LEN:
            forms |= _variants(token)
    return forms


class WindowsPlatform:
    """Реализация `PlatformCapabilities` для Windows."""

    name = "windows"
    supported_power = (
        PowerAction.SHUTDOWN,
        PowerAction.RESTART,
        PowerAction.SLEEP,
        PowerAction.LOCK,
        PowerAction.SIGN_OUT,
    )

    def list_apps(self) -> list[AppInfo]:
        """Список приложений, которые ассистент способен открыть.

        Первыми идут встроенные: их всегда можно открыть, и пользователю
        не нужно знать, установлены они или нет.
        """
        builtin = [
            AppInfo(name=label, target=exe, method=AppLaunchMethod.EXECUTABLE,
                    aliases=tuple(k for k, (e, _l) in _BUILTIN_APPS.items()
                                  if e == exe and k != _norm(label)))
            for exe, label in _BUILTIN_APPS.values()
        ]
        # Убираем дубликаты названий, сохраняя порядок.
        seen: set[str] = set()
        unique = []
        for app in builtin:
            if app.name not in seen:
                seen.add(app.name)
                unique.append(app)
        return unique + _scan_installed_apps()

    def launch_app(self, name: str) -> tuple[bool, str]:
        key = _norm(name)
        if not key:
            return False, "Не указано, что открыть"

        # 1. Явные URI-схемы (настройки, диспетчер задач, почта).
        uri = _URI_APPS.get(key)
        if uri:
            return (True, uri) if _open_uri(uri) else (False, f"Не удалось открыть {uri}")

        # 2. Встроенные приложения: блокнот, калькулятор, paint.
        builtin = _BUILTIN_APPS.get(key)
        if builtin:
            exe, label = builtin
            if _launch_path(exe.split()[0]):
                return True, label
            return False, f"Не удалось запустить {label}"

        # 3. Путь или команда, как есть.
        if _launch_path(key):
            return True, key

        # 4. Поиск среди установленных программ.
        found = self.find_app(key)
        if found:
            return True, found.target

        # 5. Нечёткий поиск — ТОЛЬКО как подсказка, никогда как действие.
        #
        # Почему: замеры на реальном списке программ показали, что верные и
        # ложные совпадения перекрываются. «стим» -> Steam даёт 0.67, и
        # «chrome» -> Heroes III Launcher тоже 0.67. Один порог их не
        # разделит, а открыть не то приложение хуже, чем спросить.
        best = self.suggest_apps(key, limit=3, min_score=_FUZZY_SUGGEST)
        if best:
            names = ", ".join(a.name for a in best)
            return False, f"Не уверен, что это «{name}». Похожее: {names}"

        return False, f"Не нашёл программу «{name}»"

    def find_app(self, query: str, *, fuzzy: bool = False) -> AppInfo | None:
        """Находит приложение по имени или одному из алиасов.

        Сначала точное совпадение по границам слов: «autocad» должен
        найти «AutoCAD 2026 — Русская версия», а «word» не должен
        найти «Password Manager».

        `fuzzy=True` включает запасной нечёткий путь. Он нужен там,
        где результат показывают человеку, а не запускают. Включать
        его по умолчанию нельзя: замеры на реальном списке показали,
        что верные и ложные совпадения перекрываются — «стим» к Steam
        и «chrome» к Heroes III Launcher дают одно и то же 0.67, —
        и открыть не то хуже, чем спросить.
        """
        query_variants = _variants(query)
        if not query_variants:
            return None

        best: tuple[int, AppInfo] | None = None

        for app in self.list_apps():
            names: set[str] = set()
            for source in (app.name, *app.aliases):
                names |= _variants(source)

            for query_form in query_variants:
                for candidate in names:
                    quality = _match_quality(query_form, candidate)
                    if quality <= 0:
                        continue
                    if best is None or quality > best[0]:
                        best = (quality, app)
                    break

        if best is not None:
            return best[1]
        if not fuzzy:
            return None
        return self._find_fuzzy(query_variants)

    def _find_fuzzy(self, query_variants: set[str]) -> AppInfo | None:
        """Запасной нечёткий поиск, когда точного совпадения нет.

        Требование отрыва от второго места не украшение: любое слово
        находит что-нибудь похожее. Если два кандидата почти равны —
        непонятно, что имелось в виду, и открывать что-то наугад хуже,
        чем спросить.
        """
        import difflib

        scored: list[tuple[float, AppInfo]] = []
        for app in self.list_apps():
            best = 0.0
            for source in (app.name, *app.aliases):
                for candidate in _name_forms(source):
                    for query_form in query_variants:
                        score = difflib.SequenceMatcher(
                            None, query_form, candidate).ratio()
                        if score > best:
                            best = score
            if best >= FUZZY_MIN_SCORE:
                scored.append((best, app))

        if not scored:
            return None

        scored.sort(key=lambda pair: pair[0], reverse=True)
        if len(scored) > 1 and scored[0][0] - scored[1][0] < FUZZY_MIN_GAP:
            log.debug("Нечёткий поиск: кандидаты почти равны (%s / %s)",
                      scored[0][1].name, scored[1][1].name)
            return None
        return scored[0][1]

    def suggest_apps(self, query: str, *, limit: int = 5,
                     min_score: float = 0.55) -> list[AppInfo]:
        """Похожие программы. Нужна для «что у тебя есть» и нечёткого ввода.

        Сравниваются ВСЕ варианты запроса со всеми вариантами названия:
        «автокад» -> «avtokad», «AutoCAD 2026» -> «autocad 2026».
        Иначе транслитерация теряется и совпадение не находится вовсе.
        """
        import difflib

        query_variants = sorted(_variants(query))
        if not query_variants:
            return []
        scored: list[tuple[float, AppInfo]] = []

        for app in self.list_apps():
            best = 0.0
            for name in (app.name, *app.aliases):
                for candidate in _name_forms(name):
                    for query_form in query_variants:
                        score = difflib.SequenceMatcher(
                            None, query_form, candidate
                        ).ratio()
                        if score > best:
                            best = score
            if best >= min_score:
                scored.append((best, app))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [app for _score, app in scored[:limit]]

    def match_score(self, query: str, app: AppInfo) -> float:
        """Оценка похожести 0..1.

        Нужна навыку, чтобы решить: доверять совпадению и открыть молча
        или спросить пользователя. Открывать не то приложение хуже, чем
        спросить.
        """
        import difflib

        best = 0.0
        query_variants = sorted(_variants(query))
        for name in (app.name, *app.aliases):
            for candidate in _name_forms(name):
                for query_form in query_variants:
                    best = max(best, difflib.SequenceMatcher(
                        None, query_form, candidate).ratio())
        return round(best, 3)

    # --- громкость --------------------------------------------------------

    def _volume_key(self, key: int, steps: int) -> bool:
        if user32 is None:
            return False
        for _ in range(max(1, min(steps, 25))):
            user32.keybd_event(key, 0, 0, 0)
            user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.01)
        return True

    def volume_up(self, steps: int = 1) -> bool:
        return self._volume_key(VK_VOLUME_UP, steps)

    def volume_down(self, steps: int = 1) -> bool:
        return self._volume_key(VK_VOLUME_DOWN, steps)

    def volume_mute(self) -> bool:
        if user32 is None:
            return False
        user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
        user32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_KEYUP, 0)
        return True

    def volume_state(self) -> VolumeState:
        """Абсолютную громкость без COM не прочитать.

        Честнее вернуть `percent=None`, чем выдумать число: иначе навык
        «какая громкость» будет называть случайную величину.
        """
        return VolumeState(percent=None, muted=False, supported=True)

    # --- медиа ------------------------------------------------------------

    def _media_key(self, key: int) -> bool:
        if user32 is None:
            return False
        user32.keybd_event(key, 0, 0, 0)
        user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
        return True

    def media_play_pause(self) -> bool:
        return self._media_key(0xB3)   # VK_MEDIA_PLAY_PAUSE

    def media_next(self) -> bool:
        return self._media_key(0xB0)   # VK_MEDIA_NEXT_TRACK

    def media_previous(self) -> bool:
        return self._media_key(0xB1)   # VK_MEDIA_PREV_TRACK

    # --- окна -------------------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        if user32 is None:
            return []

        windows: list[WindowInfo] = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            windows.append(WindowInfo(handle=hwnd, title=title))
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(callback), 0)
        except Exception:
            log.debug("EnumWindows не удался", exc_info=True)
        return windows

    def focus_window(self, handle: int) -> bool:
        if user32 is None:
            return False
        # Часто Windows запрещает переключение на окно из другого процесса.
        # Обходной путь — восстановить окно и «приподнять» его.
        user32.ShowWindow(handle, 9)  # SW_RESTORE
        user32.SetForegroundWindow(handle)
        return user32.GetForegroundWindow() == handle

    def close_window(self, handle: int) -> bool:
        if user32 is None:
            return False
        user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE
        return True

    def minimize_all(self) -> bool:
        """Свернуть все окна. Только Windows — такого сочетания нет на других ОС."""
        if user32 is None:
            return False
        shell = ctypes.WinDLL("shell32")
        return bool(shell.ShellExecuteW(None, "U", None, None, "Minimize", 0))

    # --- ввод и буфер -----------------------------------------------------

    def type_text(self, text: str) -> bool:
        """Ввести текст в активное окно через SendInput.

        Важно: вызывать только по явной команде пользователя. Это наш
        «ввод с клавиатуры», и он может попасть в чужое окно.
        """
        if not text:
            return False
        return _send_unicode_text(text)

    def clipboard_get(self) -> TextSelection:
        try:
            import pyperclip

            return TextSelection(text=pyperclip.paste() or "", available=True)
        except Exception as exc:
            return TextSelection(available=False, reason=str(exc))

    def clipboard_set(self, text: str) -> bool:
        try:
            import pyperclip

            pyperclip.copy(text)
            return True
        except Exception as exc:
            get_dialogue_logger().info("Буфер обмена недоступен: %s", exc)
            return False

    # --- экран ------------------------------------------------------------

    def screenshot(self, path: str | None = None) -> str:
        """Скриншот нажатием Win+PrintScreen, затем ищем свежий файл."""
        pictures = Path(
            os.environ.get("USERPROFILE", str(Path.home()))
        ) / "Pictures/Screenshots"

        before = _recent_files(pictures)
        if user32 is None:
            raise RuntimeError("Скриншот доступен только в Windows")

        # Win+Print
        for vk in (VK_LWIN, VK_PRINT):
            user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        for vk in (VK_PRINT, VK_LWIN):
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

        # Ждём, пока Windows доложит файл на диск.
        for _ in range(30):
            time.sleep(0.2)
            new_files = set(_recent_files(pictures)) - before
            if new_files:
                return str(max(new_files, key=lambda p: p.stat().st_mtime))
        return str(pictures)

    def set_brightness(self, percent: int) -> bool:
        """Яркость на ноутбуках. На десктопах без подсветки — нет."""
        if not _is_laptop():
            return False
        return _set_brightness_wmi(max(0, min(100, int(percent))))

    # --- система ----------------------------------------------------------

    def status(self) -> SystemStatus:
        import psutil

        disk_free_gb = None
        disk_percent = None
        try:
            usage = psutil.disk_usage(_system_drive())
            disk_free_gb = round(usage.free / (1024**3), 1)
            disk_percent = usage.percent
        except Exception:
            log.debug("Не удалось получить данные о диске", exc_info=True)

        memory_percent = memory_used_gb = memory_total_gb = None
        try:
            mem = psutil.virtual_memory()
            memory_percent = mem.percent
            memory_total_gb = round(mem.total / (1024**3), 1)
            memory_used_gb = round(mem.used / (1024**3), 1)
        except Exception:
            pass

        battery_percent = plugged = None
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                battery_percent = battery.percent
                plugged = battery.power_plugged
        except Exception:
            pass

        # psutil.boot_time() отдаёт время загрузки в секундах epoch, а не в
        # миллисекундах. Деление на 1000 давало «работает 74573543 дня».
        uptime_hours = round((time.time() - psutil.boot_time()) / 3600, 1)

        return SystemStatus(
            cpu_percent=psutil.cpu_percent(interval=0.3),
            memory_percent=memory_percent,
            memory_used_gb=memory_used_gb,
            memory_total_gb=memory_total_gb,
            disk_percent=disk_percent,
            disk_free_gb=disk_free_gb,
            battery_percent=battery_percent,
            plugged=plugged,
            uptime_hours=uptime_hours,
        )

    def open_path(self, path: str, *, reveal: bool = False) -> bool:
        target = (path or "").strip().strip('"')
        if not target:
            return False

        if reveal and Path(target).exists():
            # «Показать в проводнике» — так привычнее пользователю.
            subprocess.Popen(["explorer", "/select,", target])
            return True

        if Path(target).exists():
            return _start_file(target)

        # Может быть URL.
        if target.lower().startswith(("http://", "https://", "ftp://")):
            return _open_uri(target)

        # Иначе ищем как имя файла/папки.
        expanded = _expand_windows_path(target)
        if expanded:
            return _start_file(expanded)
        return False

    # --- питание ----------------------------------------------------------

    def power_action(self, action: PowerAction, *, delay_s: int = 0) -> bool:
        delay = max(0, int(delay_s))
        if action in _POWER_COMMANDS:
            cmd = [part.format(delay=delay) for part in _POWER_COMMANDS[action]]
            return _run_detached(cmd)

        if action is PowerAction.SLEEP:
            # SetSuspendState(0,1,0) — спящий режим (hibernate выключен).
            return _run_detached(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])

        if action is PowerAction.LOCK:
            return _run_detached(["rundll32.exe", "user32.dll,LockWorkStation"])

        if action is PowerAction.SIGN_OUT:
            return _run_detached(["shutdown", "/l"])

        return False


# --- вспомогательное --------------------------------------------------------

#: Приложения, открываемые через URI-схему, а не через файл.
#: Блокнот, Калькулятор и Проводник сюда НЕ входят: у них есть надёжные
#: исполняемые файлы (notepad.exe, calc.exe), а URI-схемы работают хуже.
_URI_APPS = {
    "настройки": "ms-settings:",
    "параметры": "ms-settings:",
    "панель управления": "control",
    "приложения и возможности": "ms-settings:appsfeatures",
    "персонализация": "ms-settings:personalization",
    "буфер обмена": "ms-settings:clipboard",
    "сеть": "ms-settings:network",
    "bluetooth": "ms-settings:bluetooth",
    "экран": "ms-settings:display",
    "звук": "ms-settings:sound",
    "обновление windows": "ms-settings:windowsupdate",
    "почта": "mailto:",
    "компьютер": "ms-settings:mycomputer",
}


def _open_uri(uri: str) -> bool:
    try:
        os.startfile(uri)  # noqa: S606 - системная схема
        return True
    except Exception:
        log.debug("Не удалось открыть %s", uri, exc_info=True)
        return False


#: Разрешённый вид имени для прямого запуска.
#:
#: Ввод пользователя нельзя передавать в `os.startfile` как есть: строка
#: вида «foo & bar» — не приложение. Пропускаем только безопасные имена.
#:
#: Разделители пути (`\`, `/`, `:`) разрешены — без них не работает
#: «открой C:\Program Files\...». Обход каталогов («..») запрещён: для
#: обычных голосовых команд он не нужен, зато позволяет выйти наружу.
_SAFE_NAME = re.compile(r"^[\w \-.:\\/]+$", re.UNICODE)
_EXECUTABLE_SUFFIXES = (".exe", ".lnk", ".bat", ".cmd", ".msc", ".com")


def _is_safe_name(name: str) -> bool:
    """Проверка имени файла перед передачей в запуск.

    Самостоятельна: сама убирает пробелы и кавычки, чтобы не зависеть от
    того, вызвал ли кто `.strip()` раньше.
    """
    name = (name or "").strip().strip('"').strip()
    if not name:
        return False
    if not _SAFE_NAME.match(name):
        return False
    if ".." in name:
        log.warning("Отклонено имя с обходом каталогов: %r", name)
        return False
    return True


def _launch_path(name: str) -> bool:
    """Пытается запустить то, что есть: путь, команда в PATH, .lnk.

    `shutil.which` проверяется ВСЕГДА. Раньше здесь стояла проверка
    «если имя уже с расширением, искать не нужно» — из-за неё
    `notepad.exe` не запускался: голое имя файла не является путём,
    и до System32 руки не доходили.
    """
    name = (name or "").strip().strip('"')
    if not _is_safe_name(name):
        return False

    # 1. Явный путь к существующему файлу.
    try:
        if Path(name).exists():
            os.startfile(name)  # noqa: S606
            return True
    except Exception:
        log.debug("Не удалось запустить %s", name, exc_info=True)
        return False

    # 2. Поиск в PATH — и с расширением, и без него.
    found = shutil.which(name)
    if not found and not name.lower().endswith(_EXECUTABLE_SUFFIXES):
        found = shutil.which(f"{name}.exe")

    if found:
        try:
            os.startfile(found)  # noqa: S606
            log.debug("Запущено из PATH: %s -> %s", name, found)
            return True
        except Exception:
            log.debug("Не удалось запустить %s", found, exc_info=True)

    return False


def _run_powershell(script: str, timeout: float = 10.0) -> str:
    """Запускает PowerShell и возвращает его вывод текстом.

    Кодировка
    ---------
    `subprocess.run(..., text=True)` расшифровывает вывод в кодировку
    системы. У нас это cp1251, а PowerShell 5.1 при перенаправлении в
    трубу пишет в консольной кодировке, где встречаются байты, не
    описанные в cp1251 (0x98, 0x99 и соседние). Декодер падает в
    фоновом потоке, `stdout` приходит как `None` — и навык падает
    вместо ответа:

        UnicodeDecodeError: 'charmap' codec can't decode byte 0x98
        AttributeError: 'NoneType' object has no attribute 'splitlines'

    Ловить это на стороне вызова бесполезно: `AttributeError` возникает
    уже во время чтения потока, минуя `except Exception`.

    Поэтому две меры сразу: заставляем PowerShell отдавать UTF-8 и
    расшифровываем с заменой нечитаемых байтов. Имена ярлыков содержат
    кириллицу — потерять их значит потерять сами ярлыки.
    """
    # Вывод в UTF-8 независимо от кодовой страницы в системе.
    preamble = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             preamble + script],
            capture_output=True, timeout=timeout,   # байты, не текст
        )
    except subprocess.TimeoutExpired:
        log.debug("PowerShell не ответил за %.0f с", timeout)
        return ""
    except OSError:
        log.debug("Не удалось запустить PowerShell", exc_info=True)
        return ""

    return (result.stdout or b"").decode("utf-8", errors="replace")


def _scan_installed_apps() -> list[AppInfo]:
    """Ищет установленные программы: ярлыки меню «Пуск» + реестр.

    Результат кэшируется: сканирование занимает секунды, а список программ
    меняется редко.

    Важно: ярлыки читаются ОДНИМ вызовом PowerShell. Если запускать
    PowerShell на каждый .lnk отдельно, на машине со 150 программами
    сканирование занимает ~100 секунд — недопустимо для запуска.
    """
    global _APPS_CACHE
    if _APPS_CACHE is not None:
        return _APPS_CACHE

    apps: dict[str, AppInfo] = {}

    for app in _shortcuts_batch():
        apps.setdefault(app.name, app)

    for app in _apps_from_registry():
        apps.setdefault(app.name, app)

    _APPS_CACHE = sorted(apps.values(), key=lambda a: a.name)
    log.info("Найдено приложений: %d", len(_APPS_CACHE))
    return _APPS_CACHE


_APPS_CACHE: list[AppInfo] | None = None


def _shortcuts_batch() -> list[AppInfo]:
    """Читает цели всех ярлыков одним запуском PowerShell.

    Разделитель `|` выбран потому, что в путях Windows не встречается,
    в отличие от пробела.
    """
    roots = [str(p) for p in _START_MENU_DIRS if p.exists()]
    if not roots:
        return []

    quoted = ", ".join("'" + r.replace("'", "''") + "'" for r in roots)
    script = (
        "$sh = New-Object -ComObject WScript.Shell\n"
        f"$roots = @({quoted})\n"
        "foreach ($root in $roots) {\n"
        "  Get-ChildItem -Path $root -Filter *.lnk -Recurse -ErrorAction SilentlyContinue |\n"
        "    ForEach-Object {\n"
        "      try {\n"
        "        $t = $sh.CreateShortcut($_.FullName).TargetPath\n"
        "        if ($t) { Write-Output ($_.FullName + '|' + $t) }\n"
        "      } catch {}\n"
        "    }\n"
        "}\n"
    )

    output = _run_powershell(script, timeout=25)

    apps: list[AppInfo] = []
    for line in output.splitlines():
        if "|" not in line:
            continue
        shortcut_str, target = line.rsplit("|", 1)
        target = target.strip()
        if not target.lower().endswith((".exe", ".lnk")):
            continue

        name = Path(shortcut_str.strip()).stem.strip()
        if not name or name.lower() in _SHORTCUT_SKIP:
            continue

        apps.append(AppInfo(
            name=name,
            target=target,
            method=AppLaunchMethod.SHORTCUT,
        ))
    return apps


#: Куда Windows пишет «установленные программы».
_REGISTRY_APP_PATHS = (
    (r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
     "HKLM"),
    (r"HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
     "HKCU"),
    (r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
     "HKLM"),
)


def _apps_from_registry() -> list[AppInfo]:
    import winreg

    found: list[AppInfo] = []
    for subkey, hive_name in _REGISTRY_APP_PATHS:
        try:
            hive = winreg.HKEY_LOCAL_MACHINE if hive_name == "HKLM" else winreg.HKEY_CURRENT_USER
            with winreg.OpenKey(hive, subkey) as root:
                index = 0
                while True:
                    try:
                        entry_name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    if not entry_name:
                        continue
                    try:
                        with winreg.OpenKey(root, entry_name) as entry:
                            path, _ = winreg.QueryValue(entry, None)
                    except OSError:
                        continue
                    if path and Path(path).exists():
                        found.append(AppInfo(
                            name=entry_name,
                            target=path,
                            method=AppLaunchMethod.EXECUTABLE,
                        ))
        except OSError:
            continue
        except Exception:
            log.debug("Ошибка чтения реестра %s", subkey, exc_info=True)
    return found


_SHORTCUT_SKIP = {
    "читать меня", "readme", "справка", "help", "обновление", "update",
    "uninstall", "удалить", "website", "сайт", "поддержка", "support",
    "license", "лицензия", "release notes", "what's new",
}


def _system_drive() -> str:
    return os.environ.get("SystemDrive", "C:") + "\\"


def _run_detached(cmd: list[str]) -> bool:
    """Запускает команду без ожидания и без окна консоли."""
    try:
        flags = 0
        if os.name == "nt":
            flags = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        subprocess.Popen(  # noqa: S603
            cmd, creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        log.exception("Не удалось выполнить: %s", cmd)
        return False


def _send_unicode_text(text: str) -> bool:
    """Вводит текст в активное окно посимвольно через SendInput."""
    if user32 is None:
        return False

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 24)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    KEYEVENTF_UNICODE = 0x0004
    inputs = []
    for char in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            item = INPUT()
            item.type = 1  # INPUT_KEYBOARD
            item.ki = KEYBDINPUT(0, ord(char), flags, 0, None)
            inputs.append(item)

    sent = user32.SendInput(len(inputs), (INPUT * len(inputs))(*inputs), ctypes.sizeof(INPUT))
    return sent == len(inputs)


def _recent_files(folder: Path) -> list[Path]:
    try:
        return list(folder.glob("*.png"))
    except Exception:
        return []


def _is_laptop() -> bool:
    output = _run_powershell(
        "(Get-CimInstance Win32_Battery) -ne $null", timeout=5)
    return output.strip().lower() == "true"


def _set_brightness_wmi(percent: int) -> bool:
    script = (
        "$m = Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods "
        "-ErrorAction Stop; "
        f"$m.WmiSetBrightness(1, {percent})"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=5, check=True,
        )
        return True
    except subprocess.TimeoutExpired:
        log.debug("PowerShell не ответил вовремя — яркость не изменена")
        return False
    except (subprocess.CalledProcessError, OSError):
        log.debug("Не удалось изменить яркость", exc_info=True)
        return False


#: Русские названия системных папок -> реальные каталоги.
#:
#: Имена соответствуют API Known Folder, поэтому работают при любой
#: языковой версии Windows и при переносе профиля. Раньше разбирались
#: только английские: навык с примером «открой папку загрузки» на
#: русской Windows не мог сработать в принципе — отвечал «не нашёл»,
#: хотя папка рядом.
_WELL_KNOWN: dict[str, str] = {
    "desktop": "Desktop",
    "рабочий стол": "Desktop",
    "документы": "Documents",
    "documents": "Documents",
    "загрузки": "Downloads",
    "downloads": "Downloads",
    "изображения": "Pictures",
    "картинки": "Pictures",
    "pictures": "Pictures",
    "музыка": "Music",
    "music": "Music",
    "видео": "Videos",
    "videos": "Videos",
    "домашняя папка": "",
    "home": "",
}


def _well_known_dir(raw: str) -> str | None:
    """Русское или английское имя системной папки -> путь."""
    key = raw.strip().strip("\\/").lower()
    if key not in _WELL_KNOWN:
        return None

    folder = _WELL_KNOWN[key]
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    target = home if not folder else home / folder
    if target.exists():
        return str(target)

    # Профиль мог быть перенесён: спрашиваем у Windows напрямую.
    script = (
        f"$p = [Environment]::GetFolderPath('{folder or 'UserProfile'}'); "
        "if ($p -and (Test-Path $p)) { Write-Output $p }"
    )
    output = _run_powershell(script, timeout=5).strip()
    return output if output and Path(output).exists() else None


#: Порог для подсказки по имени файла.
#:
#: 0,66 проверен и отвергнут: на этой границе «отчет» начинает
#: предлагать «тест.txt» — совпадение случайное, и человек открыл бы
#: не тот файл. Ложная подсказка хуже отсутствующей: отсутствие
#: заметно, ошибка же незаметна и приводит к работе с чужим документом.
#:
#: Побочный эффект строгости: «отчет» не находит «Отчёт2026.pdf».
#: Это осознанно — запрос без года неоднозначен.
PATH_SUGGEST_SCORE = 0.72


def _start_file(path: str) -> bool:
    """Открывает файл связанным приложением.

    `os.startfile` бросает исключение не только когда файла нет, но и
    когда приложение не назначено: Windows показывает диалог выбора,
    и если его закрыть — прилетает `OSError 1223`. Раньше это уходило
    наверх, и человек слышал «Что-то сломалось» вместо ответа.

    Возвращаем False: навык скажет «не удалось открыть», и это правда.
    """
    try:
        os.startfile(path)  # noqa: S606 - путь от пользователя или конфига
        return True
    except OSError as exc:
        log.info("Не удалось открыть %s: %s", path, exc)
        return False


def _fold(text: str) -> str:
    """Вид имени для сравнения: нижний регистр, «ё» приравнена к «е».

    «Ё» — отдельная буква, но человек ставит её где попало: назовёт файл
    `Отчёт`, а навык разберёт фразу как `отчет` (нормализация ради
    сопоставления с командами). Без приравнивания файл не находится
    никогда — и это выглядит как «чувствительность к регистру», хотя
    регистр тут ни при чём.
    """
    return (text or "").lower().replace("ё", "е")


def _path_forms(raw: str) -> set[str]:
    """Формы имени файла: целиком и без расширения.

    Без второго варианта поиск бесполезен: человек говорит «открой файл
    отчёт», а на диске лежит `Отчёт.docx`. Требовать расширение — значит
    требовать то, чего человек не произносил.
    """
    folded = _fold(raw.strip().strip('"'))
    if not folded:
        return set()
    forms = {folded}
    stem = os.path.splitext(folded)[0]
    if stem and stem != folded:
        forms.add(stem)
    return forms


def _user_roots() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    return [home / name for name in ("Desktop", "Documents", "Downloads",
                                     "Pictures")]


def _score_against(name: str, wanted: set[str]) -> float:
    import difflib

    forms = _path_forms(name)
    if not forms:
        return 0.0
    return max(difflib.SequenceMatcher(None, w, f).ratio()
               for w in wanted for f in forms)


def suggest_paths(query: str, *, limit: int = 3) -> list[str]:
    """Похожие файлы и папки — для ответа «не нашёл, может быть…»."""
    wanted = _path_forms(query)
    if not wanted:
        return []

    scored: list[tuple[float, str]] = []
    for root in _user_roots():
        if not root.exists():
            continue
        try:
            for found in root.rglob("*"):
                score = _score_against(found.name, wanted)
                if score >= PATH_SUGGEST_SCORE:
                    scored.append((score, found.name))
        except OSError:
            continue

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [name for _score, name in scored[:limit]]


def _expand_windows_path(raw: str) -> str | None:
    """Понимает сокращения путей: %USERPROFILE%\\Downloads, ~\\ и т.п."""
    expanded = os.path.expandvars(raw)
    if expanded.startswith("~"):
        expanded = str(Path.home() / expanded[1:].lstrip("\\/"))

    # Сначала известные папки: при перенесённом профиле простой поиск
    # по имени их не найдёт, а положить их вручную тоже не выйдет.
    known = _well_known_dir(raw)
    if known:
        return known

    path = Path(expanded)
    if path.exists():
        return str(path)

    # Поиск по имени в пользовательских папках.
    #
    # Только точное совпадение с учётом «ё» и без расширения. Ближе
    # не ищем: открывать файл по догадке — это уже не то действие,
    # о котором просил человек. Близкие имена навык показывает как
    # подсказку, и решение принимает человек.
    #
    # Два прохода, а не один: формы «1.pdf» и «1.txt» пересекаются
    # по именам без расширения, и один проход открыл бы не тот файл.
    # Сначала полное имя, затем основа — как допущение.
    folded = _fold(raw.strip().strip('"'))
    if not folded:
        return None
    whole = {folded}
    stem = {os.path.splitext(folded)[0] or folded}

    for forms in (whole, stem):
        for root in _user_roots():
            if not root.exists():
                continue
            try:
                for found in root.rglob("*"):
                    if _path_forms(found.name) & forms:
                        return str(found)
            except OSError:
                continue
    return None
    return None


def clear_app_cache() -> None:
    """Сбросить кэш приложений — после установки новой программы."""
    global _APPS_CACHE
    _APPS_CACHE = None
