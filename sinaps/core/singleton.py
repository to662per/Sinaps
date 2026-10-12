"""Защита от второго экземпляра ассистента.

Зачем это нужно
---------------
Две копии Sinaps, работающие одновременно, делят ОДИН входной аудиопоток.
Windows открывает микрофон в разделяемом режиме, и кадры достаются
копиям по очереди. На практике это выглядит так:

    15:30:15  Фраза завершена: 0.98 с
    15:31:25  Фраза завершена: 0.98 с
    ...'Субтитры субтитров Н.Новикова' (conf=0.06)
    ...'Субтитры субтитров Н.Новикова' (conf=0.04)

Фразы одинаковой длины, одинаковые выдумки, «микрофон не работает».
Пользователь ищет проблему в микрофоне, а дело в том, что ассистент
запущен дважды — из автозагрузки и вручную.

Проверка при старте дешевле, чем такой поиск.

Как сделано
-----------
Windows: именованный мьютекс (`Local\\Sinaps-<ключ>`). Ядро освобождает его
сам, когда процесс умирает, — зависшие «залоченные» состояния не нужны,
в отличие от файла-блокировки.

Остальные системы: файл с PID в каталоге проекта. Там проверяется, жив ли
процесс, иначе после аварийного завершения файл остался бы навсегда.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

#: Имя мьютекса. `Local\` — только в текущей сессии, права администратора
#: не нужны, и две копии в разных сессиях не конфликтуют.
_MUTEX_STEM = "Sinaps"

#: Файл с PID для остальных систем и для показа в сообщении.
PID_FILE = PROJECT_ROOT / "data" / "sinaps.pid"

_ERROR_ALREADY_EXISTS = 183


@dataclass(frozen=True)
class RunningInstance:
    """Информация о копии, которая уже работает."""

    pid: int | None
    started: str = ""      # "" — неизвестно


class SingleInstance:
    """Держит «право первого». Использование:

        guard = SingleInstance()
        busy = guard.acquire()
        if busy:
            print(f"Sinaps уже запущен (процесс {busy.pid})")
            return
        ...
        guard.release()
    """

    def __init__(self, *, name: str = _MUTEX_STEM) -> None:
        self.name = name
        self._handle = None
        self._owned = False

    # --- имя, уникальное для каталога проекта ------------------------------

    @property
    def _key(self) -> str:
        """Разные копии проекта в разных папках не должны конфликтовать."""
        import hashlib

        digest = hashlib.sha256(str(PROJECT_ROOT).lower().encode("utf-8"))
        return f"{self.name}-{digest.hexdigest()[:12]}"

    # --- захват ------------------------------------------------------------

    def acquire(self) -> RunningInstance | None:
        """`None` — захват удался и мы первые. Иначе — кто уже работает."""
        if sys.platform.startswith("win"):
            acquired = self._acquire_windows()
        else:
            acquired = self._acquire_file()

        if acquired:
            return None
        return RunningInstance(pid=self._read_pid())

    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR]
            create_mutex.restype = wintypes.HANDLE

            handle = create_mutex(None, False, f"Local\\{self._key}")
            already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS

            if not handle:
                # Не смогли проверить — не блокируем запуск из-за проверки.
                log.warning("Не удалось создать мьютекс (%s) — "
                            "проверка второго экземпляра отключена",
                            ctypes.get_last_error())
                return True

            if already:
                ctypes.windll.kernel32.CloseHandle(handle)
                log.info("Мьютекс %s уже занят", self._key)
                return False

            self._handle = handle
            self._owned = True
            self._write_pid()
            return True
        except Exception as exc:
            # Не блокируем запуск из-за неработающей проверки: лучше рискнуть
            # второй копией, чем не дать ассистенту стартовать вообще.
            # Но молчать нельзя — иначе выглядит как «защита есть», а её нет.
            log.warning("Проверка второго экземпляра не сработала (%s) — "
                        "запуск продолжен без неё", exc)
            return True

    def _acquire_file(self) -> bool:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        if PID_FILE.exists():
            other = self._read_pid()
            if other and _process_alive(other):
                log.info("Файл %s занят процессом %s", PID_FILE, other)
                return False
            # Процесс мёртв — файл остался после аварии, забираем себе.
            log.info("Найден старый файл %s (процесс %s мёртв)", PID_FILE, other)
        try:
            fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(str(os.getpid()))
        self._owned = True
        return True

    # --- освобождение ------------------------------------------------------

    def release(self) -> None:
        if not self._owned:
            return
        self._owned = False
        if self._handle is not None:
            import ctypes

            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                log.debug("Не удалось закрыть мьютекс", exc_info=True)
            self._handle = None
        try:
            if PID_FILE.exists() and self._read_pid() == os.getpid():
                PID_FILE.unlink()
        except OSError:
            log.debug("Не удалось удалить %s", PID_FILE, exc_info=True)

    def __enter__(self) -> "SingleInstance | None":
        return self if self.acquire() is None else None

    def __exit__(self, *_exc) -> None:
        self.release()

    # --- диагностика -------------------------------------------------------

    def _write_pid(self) -> None:
        """Запомнить свой PID — чтобы вторая копия могла показать, кто занял.

        Отказ здесь не страшен: без PID сообщение будет «Sinaps уже запущен»
        без номера процесса, работать это не мешает.
        """
        try:
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            log.debug("Не удалось записать %s", PID_FILE, exc_info=True)

    @staticmethod
    def _read_pid() -> int | None:
        try:
            text = PID_FILE.read_text(encoding="utf-8").strip()
            return int(text) if text else None
        except (OSError, ValueError):
            return None


def _process_alive(pid: int) -> bool:
    """Жив ли процесс с таким PID."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle,
                                                             ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True      # процесс есть, просто не наш
    return True


def stop_stale_pid_file() -> None:
    """Убрать PID-файл, если процесса за ним уже нет."""
    pid = SingleInstance._read_pid()  # noqa: SLF001 - служебный доступ
    if pid and not _process_alive(pid):
        try:
            PID_FILE.unlink()
            log.info("Удалён устаревший %s (процесс %s)", PID_FILE, pid)
        except OSError:
            pass


def list_instance_note(pid: int | None) -> str:
    """Строка для сообщения пользователю."""
    where = f" (процесс {pid})" if pid else ""
    return (
        f"Sinaps уже запущен{where} и держит микрофон.\n"
        f"Две копии делят один входной поток — голос будет обрываться.\n"
        f"Закрой старую копию и запусти заново.\n"
        f"Найти: tasklist /FI \"IMAGENAME eq python.exe\""
    )