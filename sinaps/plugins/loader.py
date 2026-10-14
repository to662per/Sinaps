"""Загрузка плагинов: папка с манифестом превращается в команды.

Правило номер один
------------------
**Ошибка плагина не роняет ассистент.** Не «мы постараемся не уронить»,
а буквально: любой сбой в чужом коде ловится, записывается в отчёт и не
выходит наружу. Причина простая — плагины пишут не мы, и половина из них
всегда будет с опечаткой.

Что должно выжить при любой поломке
-----------------------------------
* ядро и его собственные команды;
* другие плагины;
* уже загруженная часть сломанного плагина — тоже: если `register`
  упал после трёх команд из пяти, ни одна не должна остаться в реестре.

Откат сделан через `SkillRegistry.remove`, а не «запомнить, сколько было
до». Запоминание ломается, как только исключение приходит не из той
строки, откуда ждали: останется половина чужих команд с именами
`weather.get`, и человек будет гадать, откуда они взялись.

Про порядок загрузки
---------------------
Плагины грузятся по алфавиту имён, а не по порядку каталога. Иначе
при добавлении одного файла поменяется то, какой из двух плагинов с
одинаковым именем выиграет, — а это трудно заметить и невозможно
воспроизвести.

Про имена модулей
-----------------
Каждый плагин импортируется в собственном пространстве имён: папка
плагина добавляется в `sys.path` на время импорта и убирается сразу
после. Иначе два плагина с файлом `plugin.py` перетирали бы друг друга,
и второй грузился бы тихо чужой версией.
"""

from __future__ import annotations

import importlib
import logging
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from ..skills.base import Skill
from ..skills.registry import SkillRegistry
from .manifest import Manifest, ManifestError, load_manifest

log = logging.getLogger("sinaps.plugins")

#: Где ищем плагины по умолчанию.
PLUGINS_DIRNAME = "plugins"


@dataclass
class LoadedPlugin:
    """Плагин, который загрузился и работает."""

    manifest: Manifest
    skills: list[Skill] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(spec.name for skill in self.skills
                     for spec in skill.intents)

    @property
    def permissions(self) -> tuple[str, ...]:
        return self.manifest.permissions


@dataclass
class LoadReport:
    """Что произошло при попытке загрузить плагины.

    Отчёт, а не исключение: часть плагинов в порядке, часть с ошибкой —
    это нормальное состояние, а не катастрофа. Исключение тут было бы
    тем же, что и «падает ядро, потому что чужая опечатка».
    """

    loaded: list[LoadedPlugin] = field(default_factory=list)
    #: Имя (или путь) плагина и причина. Порядок соответствует обходу.
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.loaded)

    def summary(self) -> str:
        parts = []
        if self.loaded:
            names = ", ".join(plugin.name for plugin in self.loaded)
            parts.append(f"загружено {len(self.loaded)}: {names}")
        if self.failed:
            parts.append(f"с ошибкой {len(self.failed)}")
        return "; ".join(parts) or "плагинов не найдено"


def discover(plugins_dir: str | Path) -> list[Path]:
    """Найти папки с манифестом.

    Порядок по имени папки: иначе добавление одного плагина изменит то,
    какой из двух одноимённых загрузится первым.
    """
    root = Path(plugins_dir)
    if not root.exists():
        return []

    found = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_dir() and (entry / "manifest.yaml").exists():
            found.append(entry)
    return found


def _import_entry(folder: Path, manifest: Manifest):
    """Импортировать модуль из `entry` и достать функцию.

    Модуль ищется только в папке плагина: путь добавляется в
    `sys.path` на время импорта и убирается сразу. Иначе два плагина с
    одинаковым именем файла перетирали бы друг друга тихо, и второй
    загрузился бы чужой версией.
    """
    module_name = manifest.module
    folder_str = str(folder)

    sys.path.insert(0, folder_str)
    try:
        # Кэш импортов сбрасываем: без этого повторная загрузка в том
        # же процессе взяла бы прошлую версию модуля из памяти.
        sys.modules.pop(module_name, None)
        try:
            module = importlib.import_module(module_name)
        finally:
            sys.modules.pop(module_name, None)

        if module_name in sys.modules:  # pragma: no cover - защита
            del sys.modules[module_name]
    finally:
        if folder_str in sys.path:
            sys.path.remove(folder_str)

    if not hasattr(module, manifest.function):
        raise ManifestError(
            f"{folder}: в модуле «{module_name}» нет функции "
            f"«{manifest.function}», хотя она указана в entry"
        )
    return getattr(module, manifest.function)


def _namespace(skill: Skill, plugin_name: str) -> Skill:
    """Добавить префикс плагина к именам его команд.

    Имя намерения собирается как `навык.команда`, поэтому префикс не
    всегда можно поставить переименованием навыка: если автор назвал
    навык так же, как плагин, получилось бы `weather.weather.get`.

    Правило простое: если префикса ещё нет — ставим, если есть —
    не трогаем. Плагин не может «забыть» префикс: забыть тут нечего,
    а лишний раз поставить нельзя.

    Список команд пересобирается, потому что `IntentSpec` создан с
    прежним именем и сам себя не переименует.
    """
    marker = f"{plugin_name}."
    for spec in skill.intents:
        if not spec.name.startswith(marker):
            spec.name = f"{plugin_name}.{spec.name}"
    return skill


def _drop_conflicting(registry: SkillRegistry, plugin_name: str,
                      skills: list[Skill], before: set[str]) -> list[str]:
    """Убрать команды, которые совпали с уже занятыми.

    Снимок `before` снимается ДО вызова `register`. Снять его здесь
    было ошибкой: к этому моменту реестр уже содержит команды самого
    плагина, и каждая из них считалась занятой самой собой — плагин
    грузился, а его команды молча выбрасывались.

    Конфликт не роняет плагин: теряется одна команда из пяти, а не всё.
    Возвращает имена выброшенных команд — их покажем автору.
    """
    taken = set(before)
    dropped: list[str] = []

    for skill in skills:
        for spec in skill.intents:
            if spec.name in taken:
                dropped.append(spec.name)
                skill.intents.remove(spec)
            else:
                taken.add(spec.name)

    if dropped:
        # Шаблоны пересобираются один раз, после всех выбросов.
        registry.add_all([s for s in skills if s.intents] or skills)
        log.warning("Плагин %s: команды %s заняты, пропущены",
                    plugin_name, ", ".join(dropped))
    return dropped


def load_plugin(folder: str | Path, registry: SkillRegistry,
                taken_names: set[str] | None = None) -> LoadedPlugin:
    """Загрузить один плагин.

    Бросает `ManifestError` при любой проблеме — вызывающий решает, что
    с ней делать. Само исключение не должно вылететь в конвейер.
    """
    path = Path(folder)
    manifest = load_manifest(path / "manifest.yaml")

    if taken_names is not None:
        if manifest.name in taken_names:
            raise ManifestError(
                f"{path}: плагин «{manifest.name}» уже загружен. "
                "Имя должно быть уникальным — переименуйте папку и name."
            )

    entry = _import_entry(path, manifest)
    if not callable(entry):
        raise ManifestError(
            f"{path}: {manifest.entry} — это не функция"
        )

    # Два снимка, и они отвечают на разные вопросы.
    #
    # `before_names` — занятые имена команд. По нему определяется,
    # что команда плагина столкнулась с чужой.
    #
    # `before_skills` — сами навыки. По нему делается откат: сравнивать
    # по именам команд нельзя, навык без единой команды не попал бы
    # ни в одно множество и остался бы в реестре навсегда.
    before_names = set(registry.spec_names())
    before_skills = list(registry.skills)

    try:
        result = entry(registry)
    except Exception as exc:  # noqa: BLE001 — чужой код
        _rollback(registry, before_skills)
        raise ManifestError(
            f"{path}: register упал — {type(exc).__name__}: {exc}"
        ) from exc

    if result is not None and not isinstance(result, (Skill, list, tuple)):
        _rollback(registry, before_skills)
        raise ManifestError(
            f"{path}: register вернул {type(result).__name__}. "
            "Ожидался Skill, список навыков или ничего."
        )

    # Навыки собираем и по возврату, и по тому, что появилось в реестре:
    # плагин может и вернуть навык, и добавить его напрямую. Раньше
    # здесь стояло `_collect_skills(entry)` — вызов функции вместо её
    # результата, и список всегда был пустым.
    staged: list[Skill] = []
    for candidate in _collect_skills(result):
        if not isinstance(candidate, Skill):
            raise ManifestError(
                f"{path}: register вернул {type(candidate).__name__}, "
                "ожидался Skill. Навыки создаются через Skill(name)"
            )
        staged.append(_namespace(candidate, manifest.name))

    # Навыки, которые плагин добавил прямо в реестр, а не вернул.
    # Находим их по именам команд: появились после снимка — значит
    # добавил плагин. Перебирать все навыки и префиксовать их означало
    # бы переименовать и собственные навыки ядра.
    fresh = set(registry.spec_names()) - before_names
    for skill in registry.skills:
        if any(spec.name in fresh for spec in skill.intents):
            _namespace(skill, manifest.name)
            if skill not in staged:
                staged.append(skill)

    dropped = _drop_conflicting(registry, manifest.name, staged, before_names)

    plugin = LoadedPlugin(manifest=manifest, skills=staged)
    if taken_names is not None:
        taken_names.add(manifest.name)
    log.info("Плагин %s %s: %d команд, прав: %s", manifest.name,
             manifest.version, len(plugin.commands),
             manifest.describe_permissions())
    if manifest.unknown_fields:
        log.info("Плагин %s: неизвестные поля %s (не ошибка)",
                 manifest.name, ", ".join(manifest.unknown_fields))
    if dropped:
        log.info("Плагин %s: команда %s не попала в реестр",
                 manifest.name, ", ".join(dropped))
    return plugin


def _collect_skills(result) -> list[Skill]:
    """Что `register` мог вернуть."""
    if result is None:
        return []
    if isinstance(result, Skill):
        return [result]
    if isinstance(result, (list, tuple)):
        return list(result)
    return []


def _rollback(registry: SkillRegistry, before: list[Skill]) -> None:
    """Снять всё, что плагин успел добавить.

    Сравнение по самим навыкам, а не по именам команд. Имена не годятся:
    навык, добавленный без единой команды, не попал бы ни в одно
    множество и остался бы в реестре навсегда — а это ровно тот мусор,
    ради которого откат и нужен.
    """
    for skill in registry.skills:
        if not any(skill is old for old in before):
            registry.remove(skill)


def load_all(plugins_dir: str | Path, registry: SkillRegistry) -> LoadReport:
    """Загрузить все плагины из папки.

    Ни одно исключение отсюда не выходит: неудачный плагин — это запись
    в отчёте, а не падение. Ассистент должен работать с теми плагинами,
    которые удалось, и сказать, какие не загрузились и почему.
    """
    report = LoadReport()
    taken: set[str] = set()

    for folder in discover(plugins_dir):
        try:
            report.loaded.append(load_plugin(folder, registry, taken))
        except ManifestError as exc:
            report.failed.append((folder.name, str(exc)))
            log.error("Плагин %s не загружен: %s", folder.name, exc)
        except Exception as exc:  # noqa: BLE001 — чужой код
            # Сюда попадает то, чего ManifestError не покрывает:
            # например, исключение на этапе импорта чужого модуля.
            detail = "".join(traceback.format_exception_only(type(exc), exc))
            report.failed.append((folder.name, f"{type(exc).__name__}: {exc}"))
            log.error("Плагин %s не загружен, неожиданная ошибка:\n%s",
                      folder.name, detail)

    log.info("Плагины: %s", report.summary())
    return report
