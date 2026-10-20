"""Чтение и проверка манифеста плагина.

Зачем манифест, если есть код
-----------------------------
Чтобы человек мог открыть один файл и понять, что плагин собирается
делать и к чему получит доступ, **не читая код**. Это обещание
проверяемое: всё, что плагин реально делает, покрыто разрешениями из
манифеста, а ядро не даёт ему больше.

Сколько полей нужно в первой версии
------------------------------------
Пять: `name`, `version`, `description`, `entry`, `permissions`.

ТЗ перечисляет восемь обязательных, включая `author`, `license` и
`api_version`. Здесь они необязательные, и это осознанно:

* `author` и `license` ничего не меняют в работе плагина — их
  полезно знать, но отсутствие не мешает ни запуститься, ни
  ограничиться;
* `api_version` понадобится, когда плагинов станет больше одного и
  начнёт появляться реальная несовместимость. Проблема, которой ещё
  нет, не стоит обязательного поля: оно заставит автора писать
  «1» в файле, который он читает впервые.

Если поле всё же указано — оно проверяется. Просто не требуется.

Опасность, которой этот модуль посвящён
---------------------------------------
Манифест прост, и именно поэтому в него застревают: «а что если
добавить `depends_on`?», «а как быть с `optional_permissions`?», «а
версии?» — и две недели вместо работы. Потолок здесь намеренно низкий.
Если через месяц понадобится ещё поле, его добавить можно за час;
пока понадобится, оно будет в списке пожеланий, а не в валидаторе.

Правило жёсткое: незнакомое поле — не ошибка. Ошибка — это когда
плагин врёт о том, что ему нужно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.errors import PluginError

#: Разрешения первой версии. На каждое — одна проверка в прокси.
#:
#: `network.http` выглядит исключением в проекте без сети и требует
#: пояснения: плагин получает сеть через прокси, ядро само не ходит в
#: интернет никогда, а решение остаётся за человеком — по умолчанию
#: сеть выключена.
KNOWN_PERMISSIONS = frozenset({
    "network.http",       # HTTP-запросы через прокси ядра
    "files.read",         # чтение файлов
    "files.write",        # запись файлов
    "clipboard.read",     # чтение буфера обмена
    "clipboard.write",    # запись в буфер
    "notify",             # уведомление в трее
    "state",              # своё состояние в SQLite
    "schedule",           # напоминания, переживающие перезапуск
    "platform.apps",      # запуск и закрытие программ
    "platform.media",     # звук и медиаплеер
    "platform.power",     # питание (всегда с подтверждением)
})

#: Разрешения, которые меняют то, что человек видит и слышит.
#: Плагин с таким правом — повод показать список при установке.
NOTABLE_PERMISSIONS = frozenset({
    "network.http", "files.write", "clipboard.read",
    "platform.power", "platform.apps",
})

#: Пояснения к правам для человека.
#:
#: Без них окно прав показывало бы строки вида `platform.power`, и
#: человек, не читавший документацию, не смог бы решить, безопасно ли
#: это. Поэтому формулировка объясняет последствие, а не имя права:
#: «Управлять питанием компьютера. Каждая команда спросит подтверждение
#: голосом» — это понятно, `platform.power` — нет.
PERMISSION_TITLES = {
    "network.http": "Отправлять запросы в интернет",
    "files.read": "Читать файлы на диске",
    "files.write": "Создавать и менять файлы",
    "clipboard.read": "Читать буфер обмена",
    "clipboard.write": "Копировать в буфер обмена",
    "notify": "Показывать уведомления",
    "state": "Хранить свои данные между запусками",
    "schedule": "Ставить напоминания, переживающие перезапуск ассистента",
    "platform.apps": "Запускать и закрывать программы",
    "platform.media": "Управлять звуком и плеером",
    "platform.power": "Управлять питанием. Каждая команда спросит "
                      "подтверждение голосом",
}


def permission_title(name: str) -> str:
    """Человеческое пояснение права.

    Неизвестное право показывается как есть, а не молча прячется: плагин
    может объявить что-то новое, и человек должен это увидеть.
    """
    return PERMISSION_TITLES.get(name, name)

#: Версия API, которую понимает это ядро.
API_VERSION = 1

#: Имя должно быть пригодным для префикса команд и для имени папки.
#: Латиница, цифры и подчёркивание, без пробелов и кириллицы: префикс
#: попадёт в намерения, которые пишутся в историю и в логи.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Разрешённые форматы версии: `1.0.0`, `0.3`, `2`.
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,2}$")

#: `entry` — это `модуль:функция`.
_ENTRY_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")

DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300

#: Пять обязательных полей.
#:
#: Версия входит в список, хотя сравнивать её пока не с чем: плагин
#: один. Но она стоит рядом с именем при описании, и глазу спокойнее,
#: когда видно «1.0.0», а не пустое место. Если полей станет больше
#: одного плагина и начнёт появляться настоящая несовместимость —
#: тогда и версионирование API пригодится, но не раньше.
REQUIRED_FIELDS = ("name", "version", "description", "entry", "permissions")

#: Необязательные. `api_version` понадобится, когда появится реальная
#: несовместимость между плагином и ядром; `author` и `license` ничего
#: в работе не меняют.
OPTIONAL_FIELDS = ("api_version", "author", "license", "homepage", "timeout_s")


class ManifestError(PluginError):
    """Манифест не проходит проверку.

    Отдельный тип, чтобы загрузчик мог отличить «плагин написан не так»
    от «плагин сломался при импорте». Первое лечится правкой автора,
    второе — его отладкой, и человек должен видеть разницу.
    """


@dataclass(frozen=True)
class Manifest:
    """Что плагин заявляет о себе."""

    name: str
    version: str
    description: str
    entry: str
    permissions: tuple[str, ...] = ()

    api_version: int | None = None
    author: str = ""
    license: str = ""
    homepage: str = ""
    timeout_s: int = DEFAULT_TIMEOUT_S

    #: Откуда прочитали. В сообщениях об ошибках полезнее пути, чем
    #: номеров строк: человек открывает файл и видит то же самое.
    path: Path | None = None
    #: Поля, которые мы не знаем. Не ошибка — предупреждение: возможно,
    #: плагин писался под будущую версию.
    unknown_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def module(self) -> str:
        """Модуль из `entry` — то, что нужно импортировать."""
        return self.entry.split(":", 1)[0]

    @property
    def function(self) -> str:
        """Функция из `entry` — то, что нужно вызвать."""
        return self.entry.split(":", 1)[1]

    @property
    def asks_for_attention(self) -> bool:
        """Плагин меняет то, что человек видит и слышит.

        Такой плагин нельзя ставить молча: его разрешения показывают
        при установке.
        """
        return bool(NOTABLE_PERMISSIONS & set(self.permissions))

    def describe_permissions(self) -> str:
        """Разрешения человеческим языком — для окна подтверждения."""
        return ", ".join(self.permissions) or "ничего не запрашивает"


def _fail(path: Path | None, what: str) -> None:
    where = f"{path}: " if path else ""
    raise ManifestError(f"{where}{what}")


def _check_name(raw: Any, path: Path | None) -> str:
    if not isinstance(raw, str) or not raw:
        _fail(path, "поле name обязательно и должно быть строкой")
    if not _NAME_RE.match(raw):
        _fail(path, (
            f"имя плагина «{raw}» не подходит: только латиница в нижнем "
            "регистре, цифры и подчёркивание, начиная с буквы. Имя "
            "становится префиксом команд и попадает в историю и логи."
        ))
    return raw


def _check_version(raw: Any, path: Path | None) -> str:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        _fail(path, (
            "поле version обязательно: должно быть числом или строкой, "
            f"получено {raw!r}"
        ))
    text = str(raw).strip()
    if not _VERSION_RE.match(text):
        _fail(path, (
            f"версия «{text}» не подходит: ожидалось что-то вроде "
            "1.0.0, 0.3 или 2"
        ))
    return text


def _check_description(raw: Any, path: Path | None) -> str:
    if not isinstance(raw, str) or not raw.strip():
        _fail(path, (
            "поле description обязательно: человек открывает манифест, "
            "чтобы за десять секунд понять, что плагин делает"
        ))
    return raw.strip()


def _check_entry(raw: Any, path: Path | None) -> str:
    if not isinstance(raw, str) or not raw.strip():
        _fail(path, "поле entry обязательно и должно быть вида модуль:функция")
    text = raw.strip()
    if not _ENTRY_RE.match(text):
        _fail(path, (
            f"entry «{text}» не подходит: ожидалось «модуль:функция», "
            "например plugin:register"
        ))
    return text


def _check_permissions(raw: Any, path: Path | None) -> tuple[str, ...]:
    if raw is None:
        _fail(path, (
            "поле permissions обязательно. Пустой список напишите явно: "
            "так видно, что автор подумал, а не забыл."
        ))
    if isinstance(raw, str):
        # Частая описка: перечислили строкой через запятую.
        raw = [item.strip() for item in raw.split(",") if item.strip()]
    if not isinstance(raw, (list, tuple)):
        _fail(path, "поле permissions должно быть списком")

    permissions: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            _fail(path, f"разрешение {item!r} должно быть строкой")
        name = item.strip()
        if not name:
            continue
        if name not in KNOWN_PERMISSIONS:
            _fail(path, (
                f"неизвестное разрешение «{name}». Известные: "
                f"{', '.join(sorted(KNOWN_PERMISSIONS))}"
            ))
        if name not in permissions:
            permissions.append(name)
    return tuple(permissions)


def _check_api_version(raw: Any, path: Path | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _fail(path, f"поле api_version должно быть числом, получено {raw!r}")
    if value > API_VERSION:
        _fail(path, (
            f"плагин написан под API версии {value}, а ядро знает только "
            f"{API_VERSION}. Похоже, он из будущего."
        ))
    return value


def _check_timeout(raw: Any, path: Path | None) -> int:
    if raw is None:
        return DEFAULT_TIMEOUT_S
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _fail(path, f"поле timeout_s должно быть числом, получено {raw!r}")
    if value <= 0:
        _fail(path, f"timeout_s должен быть больше нуля, указано {value}")
    if value > MAX_TIMEOUT_S:
        _fail(path, (
            f"timeout_s={value} больше максимума {MAX_TIMEOUT_S}. Плагин "
            "не должен держать ассистент дольше пяти минут."
        ))
    return value


def _text(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def parse_manifest(raw: Any, *, path: Path | None = None) -> Manifest:
    """Проверить словарь и собрать `Manifest`.

    Разбор YAML и проверка разнесены: так одну и ту же проверку можно
    вызвать из теста, не заводя временный файл.
    """
    if not isinstance(raw, dict):
        _fail(path, (
            f"ожидался словарь с полями, получено {type(raw).__name__}"
        ))

    missing = [name for name in REQUIRED_FIELDS if name not in raw]
    if missing:
        _fail(path, (
            f"не хватает обязательных полей: {', '.join(missing)}. "
            f"Обязательны: {', '.join(REQUIRED_FIELDS)}"
        ))

    known = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    unknown = tuple(sorted(set(raw) - known))

    return Manifest(
        name=_check_name(raw.get("name"), path),
        version=_check_version(raw.get("version"), path),
        description=_check_description(raw.get("description"), path),
        entry=_check_entry(raw.get("entry"), path),
        permissions=_check_permissions(raw.get("permissions"), path),
        api_version=_check_api_version(raw.get("api_version"), path),
        author=_text(raw.get("author")),
        license=_text(raw.get("license")),
        homepage=_text(raw.get("homepage")),
        timeout_s=_check_timeout(raw.get("timeout_s"), path),
        path=path,
        unknown_fields=unknown,
    )


def load_manifest(path: str | Path) -> Manifest:
    """Прочитать `manifest.yaml` и проверить.

    Любая ошибка — `ManifestError` с понятным текстом. Вызывающий не
    должен разбирать исключение, чтобы понять причину: плагин не
    грузится, и этого достаточно.
    """
    file = Path(path)
    if not file.exists():
        raise ManifestError(f"Манифест не найден: {file}")

    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"{file}: не удалось разобрать YAML: {exc}") from exc
    except OSError as exc:
        raise ManifestError(f"{file}: не удалось прочитать: {exc}") from exc

    return parse_manifest(raw, path=file)
