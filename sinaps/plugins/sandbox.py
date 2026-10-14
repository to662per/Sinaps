"""Запрет сети в процессе плагина.

Что здесь и зачем
-----------------
Ядро Sinaps в сеть не ходит никогда, и это проверяется тестом на
источниках. Но дочерний процесс плагина — это **чужой код**, и на
исполняемое разрешение `network.http` опираться нельзя: автор мог
импортировать `urllib` где угодно и не заметить.

Поэтому запрет сделан до того, как начнёт исполняться код плагина, и
до того, как он что-либо импортирует: импорт перехватывается на
`sys.meta_path` и для запрещённого имени бросается ошибка. Ни флага,
ни проверки «после вызова» — их можно обойти, забыв про них один раз.

Почему не хватает запрета на `socket` «как есть»
------------------------------------------------
Первая версия закрывала только клиентские библиотеки и оставляла
`socket`. Измерение показало, что этого мало: из сокета открывается
любой TCP, а `ctypes` даёт WinHTTP и WinINet — мимо Python вовсе.
Закрыт и `subprocess`, иначе `curl` из плагина снимает любой запрет.

Но `socket` закрыть нельзя было трижды подряд, пока не выяснилось
почему: **на Windows `asyncio` импортирует `socket`, а цикл событий
нужен для вызова обработчика**. Отсюда порядок, который выглядит
странно:

  1. ребёнок сначала грузит `asyncio` и витрину API;
  2. затем вычищает из `sys.modules` модули, которые сам же только что
     импортировал, — иначе `import socket` у плагина попал бы в кэш и
     разблокировался;
  3. и только потом ставит запрет.

Проверено измерением, а не рассуждением: после этого `asyncio.run`
работает, `socket` не импортируется, файлы и sqlite работают.

Чего это НЕ даёт
----------------
Импорты Python перехватываются, а обращения из C — нет. Нативное
расширение, умеющее открывать сокет напрямую, мимо этого запрета
пройдёт. Об этом написано в документации, а не убрано в формулировку
«сети нет»: обещание шире того, что проверено.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("sinaps.plugins.sandbox")

#: Корни модулей, которые в процессе плагина недоступны.
#:
#: Сравнение по корню, а не по полному имени: `urllib.request` и
#: `urllib.error` приходят из одного пакета, и перечислять подмодули
#: поштучно — значит забыть один.
BLOCKED_ROOTS = frozenset({
    # низкий уровень
    "socket",
    "ssl",
    "ctypes",
    "_ctypes",
    "subprocess",
    # клиенты HTTP
    "urllib",
    "http",
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "websockets",
    # прочее
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "xmlrpc",
    # тянущие веса мимо вопроса
    "huggingface_hub",
})

#: Модули, которые надо вычистить из кэша перед постановкой запрета.
#:
#: Если этого не сделать, запрет не сработает ни на что: `import socket`
#: найдёт модуль в `sys.modules` и не дойдёт до перехватчика. Именно
#: так выглядел неработающий запрет в первой версии — тест на
#: отсутствующем кэше проходил, на живом коде нет.
_EVICT = ("socket", "ssl", "_socket", "selectors",
          "ctypes", "_ctypes", "subprocess")


class _NetworkGate:
    """Перехватчик импорта. Возвращает `None` для всего разрешённого."""

    __slots__ = ("roots",)

    def __init__(self, roots: frozenset[str]) -> None:
        self.roots = roots

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.roots:
            from ..core.errors import NetworkDisabled

            raise NetworkDisabled(
                f"модуль «{name}». Нужно разрешение network.http, "
                "которое ещё не одобрено."
            )
        return None


#: Ставился ли запрет. Повторная постановка ничего не меняет, но
#: повторная вычистка кэша ломает уже работающий цикл событий.
_armed = False


def arm(roots: frozenset[str] | None = None) -> None:
    """Поставить запрет на сеть.

    Вызывать **после** того, как загружены `asyncio` и витрина API, и
    **до** того, как начнёт исполняться код плагина. Подробности в
    модуле — порядок здесь не косметика.
    """
    global _armed
    if _armed:
        return

    banned = BLOCKED_ROOTS if roots is None else roots
    for name in _EVICT:
        if name in banned:
            sys.modules.pop(name, None)

    sys.meta_path.insert(0, _NetworkGate(banned))
    _armed = True
    log.info("Сеть в процессе плагина закрыта: %d модулей",
             len(banned))


def is_armed() -> bool:
    return _armed


def verify() -> dict[str, bool]:
    """Проверить, что запрет действительно держит.

    Не «должен держать», а проверить: сначала правило, потом
    предположение. Возвращает словарь имя → запрещён ли.

    Проверка идёт на живом кэше импортов, а не на разборе списка:
    перехватчик, который по какой-то причине не встал, даст
    `False`, и об этом узнает ядро, а не пользователь через месяц.
    """
    from ..core.errors import NetworkDisabled

    report: dict[str, bool] = {}
    for name in ("socket", "ssl", "ctypes", "urllib.request", "http.client"):
        try:
            __import__(name)
        except NetworkDisabled:
            report[name] = True
        except ImportError:
            report[name] = True      # модуля нет — сети тоже нет
        except Exception:
            report[name] = False
        else:
            report[name] = False

    if not all(report.values()):
        open_names = [name for name, closed in report.items() if not closed]
        log.error("Запрет сети не держит, доступны: %s", ", ".join(open_names))
    return report


def describe() -> str:
    """Человеческое описание — для панели возможностей."""
    if _armed:
        return f"сеть закрыта, запрещено модулей: {len(BLOCKED_ROOTS)}"
    return "сеть закрыта"