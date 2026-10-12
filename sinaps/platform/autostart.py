"""Автозапуск при входе в Windows.

Выбран реестр, а не ярлык в `shell:startup`:

  * ярлык — это файл, который пользователь случайно удалит, перетащит
    или переименует; о нём никто не знает;
  * реестр переживает переустановку, но всё равно привязан к пути,
    поэтому путь к `pythonw.exe` пишется АБСОЛЮТНЫМ. Относительный путь
    в автозапуске не работает: рабочим каталогом там будет System32.

Ветка `HKCU`, а не `HKLM`: права администратора не нужны, и пользователь
может отключить запуск безопасно.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: Куда пишем. RunOnce — выполнится один раз, Run — каждый вход.
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _value_name(project_root: Path | None = None) -> str:
    """Имя записи. Несколько копий проекта не должны затирать друг друга."""
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent.parent
    # Хэш пути: два проекта в разных папках получат разные записи.
    import hashlib

    digest = hashlib.sha256(str(root).lower().encode("utf-8")).hexdigest()[:8]
    return f"Sinaps-{digest}"


def _pythonw() -> str:
    """Путь к интерпретатору без окна консоли."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return str(exe)
    sibling = exe.with_name("pythonw.exe")
    if sibling.exists():
        return str(sibling)
    # pythonw может отсутствовать (редко на Windows). Тогда запускаем
    # обычный python с ключом -w — окно консоли не появится.
    return ""


def _command(project_root: Path | None = None) -> str:
    """Полная строка запуска, которую записываем в реестр."""
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent.parent
    pythonw = _pythonw()
    module = f'"{pythonw}"' if pythonw else f'"{sys.executable}" -w'
    return f'{module} -m sinaps'


def _winreg():
    try:
        import winreg

        return winreg
    except ImportError:        # pragma: no cover - вне Windows
        return None


def is_enabled(project_root: Path | None = None) -> bool:
    """Включён ли автозапуск."""
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _value_name(project_root))
            return bool(value)
    except (FileNotFoundError, OSError):
        return False
    except Exception:
        log.debug("Автозапуск: проверка не удалась", exc_info=True)
        return False


def enable(project_root: Path | None = None) -> bool:
    """Включить автозапуск. False — не получилось."""
    winreg = _winreg()
    if winreg is None:
        log.warning("Автозапуск доступен только в Windows")
        return False

    command = _command(project_root)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _value_name(project_root), 0,
                              winreg.REG_SZ, command)
        log.info("Автозапуск включён: %s", command)
        return True
    except OSError as exc:
        log.warning("Автозапуск: запись не удалась (%s)", exc)
        return False


def disable(project_root: Path | None = None) -> bool:
    """Выключить автозапуск."""
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _value_name(project_root))
        log.info("Автозапуск выключен")
        return True
    except FileNotFoundError:
        return True          # уже выключен — это не ошибка
    except OSError as exc:
        log.warning("Автозапуск: удаление не удалось (%s)", exc)
        return False


def set_enabled(enabled: bool, project_root: Path | None = None) -> bool:
    """Включить или выключить. True — состояние изменилось."""
    return enable(project_root) if enabled else disable(project_root)