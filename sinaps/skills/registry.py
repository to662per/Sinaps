"""Реестр навыков.

Отвечает за три вещи:
  * сопоставление текста с командой (NLU-0, без LLM);
  * проверку и разбор слотов;
  * генерацию справки «что ты умеешь».

Порядок проверки важен: команды сначала сортируются по приоритету, затем
по длине шаблона. Длинные и специфичные шаблоны должны проверяться раньше
коротких общих, иначе «открой блокнот» перехватит шаблон «открой».
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..core.errors import InvalidSlot, SinapsError, SkillError, SkillNotFound
from ..nlu.normalize import cleanup, normalize
from ..nlu.slots import (
    parse_datetime_slot,
    parse_duration,
    parse_number,
    parse_time,
)
from .base import IntentSpec, Response, Skill, SlotSpec

log = logging.getLogger(__name__)


@dataclass
class Match:
    """Результат сопоставления: какая команда и с какими слотами."""

    spec: IntentSpec
    raw_slots: dict[str, str]
    slots: dict[str, Any]
    score: float = 1.0
    matched_text: str = ""


class SkillRegistry:
    """Все навыки приложения."""

    def __init__(self) -> None:
        self._skills: list[Skill] = []
        self._specs: list[IntentSpec] = []
        self._patterns: list[tuple[re.Pattern, IntentSpec]] = []

    # --- регистрация ------------------------------------------------------

    def add(self, skill: Skill) -> Skill:
        self._skills.append(skill)
        for spec in skill.intents:
            if any(existing.name == spec.name for existing in self._specs):
                log.warning("Дубликат команды '%s' — пропускаю", spec.name)
                continue
            self._specs.append(spec)
        self._rebuild_patterns()
        return skill

    def add_all(self, skills: list[Skill]) -> None:
        for skill in skills:
            self.add(skill)

    def remove(self, skill: Skill) -> bool:
        """Убрать навык вместе со всеми его командами.

        Нужно в двух местах. Первое — откат неудачной загрузки плагина:
        если `register` упал на середине, его команды не должны остаться
        в реестре. Второе — отключение плагина из трея без перезапуска
        ассистента.

        Возвращает `False`, если навыка в реестре не было: удалять
        нечего, и это не ошибка — вызывающий сам решит, что делать.
        """
        if skill not in self._skills:
            return False

        self._skills.remove(skill)
        names = {spec.name for spec in skill.intents}
        self._specs = [spec for spec in self._specs if spec.name not in names]
        self._rebuild_patterns()
        return True

    def spec_names(self) -> tuple[str, ...]:
        """Все зарегистрированные намерения — для проверки конфликтов."""
        return tuple(spec.name for spec in self._specs)

    def _rebuild_patterns(self) -> None:
        """Собирает общий список шаблонов в порядке проверки.

        Сортировка: сначала приоритет, потом длина шаблона (длинный — раньше).
        Без этого «открой блокнот» может быть перехвачено шаблоном «открой».
        """
        pairs: list[tuple[int, int, re.Pattern, IntentSpec]] = []
        for spec in self._specs:
            for pattern in spec._compiled:
                pairs.append((spec.priority, len(pattern.pattern), pattern, spec))

        pairs.sort(key=lambda item: (-item[0], -item[1]))
        self._patterns = [(pattern, spec) for _p, _l, pattern, spec in pairs]

    # --- сопоставление ----------------------------------------------------

    @property
    def specs(self) -> list[IntentSpec]:
        return list(self._specs)

    @property
    def skills(self) -> list[Skill]:
        return list(self._skills)

    def __len__(self) -> int:
        return len(self._specs)

    def match(self, text: str, original: str | None = None) -> Match | None:
        """Находит команду по тексту.

        `original` — реплика «как её сказал пользователь». Нужна, чтобы
        достать из неё имена файлов: в нормализованном тексте «ё» уже
        заменена, а «отчёт.txt» и «отчет.txt» — разные файлы.

        Нормализация вызывается тут же, а не только вызывающим кодом.
        Раньше здесь стоял голый `text.strip().lower()`, и вызов без
        нормализации давал опасные промахи: «Выключи компьютер?» не
        проходил по `^выключи компьютер$` из-за вопросительного знака и
        уезжал в более общий шаблон `apps.close` — то есть выключение
        компьютера превращалось в закрытие приложения. Повторный вызов
        нормализации безвреден.
        """
        if not text or not text.strip():
            return None

        normalized = normalize(text)
        if not normalized:
            return None
        source = (original if original is not None else normalized).strip().lower()

        for pattern, spec in self._patterns:
            found = pattern.search(normalized)
            if not found:
                continue

            raw_slots = found.groupdict()

            # Для путей и имён файлов слот достаём из исходного текста.
            if any(s.type == "path" for s in spec.slots.values()):
                raw_slots = self._slots_from_original(spec, source) or raw_slots

            try:
                slots = self.parse_slots(spec, raw_slots, normalized)
            except InvalidSlot:
                # Слот не распознан — команда подходит, но нужно уточнение.
                return Match(spec=spec, raw_slots=raw_slots,
                             slots={}, score=0.5, matched_text=found.group(0))
            return Match(spec=spec, raw_slots=raw_slots, slots=slots,
                         matched_text=found.group(0))

        return None

    def has_missing_slots(self, match: Match) -> list[SlotSpec]:
        """Обязательные слоты, которые не удалось заполнить.

        Проверка именно на `None`, а не на «ложь». Раньше здесь стояло
        `not match.slots.get(...)`, и ноль считался пустым: «поставь
        громкость на 0» доходило до навыка с правильным числом и
        отвечало «нужно percent». Ноль — полноценное значение,
        в том числе для громкости.
        """
        missing = []
        for slot in match.spec.slots.values():
            if not slot.required:
                continue
            if match.slots.get(slot.name) is None:
                missing.append(slot)
        return missing

    def _slots_from_original(self, spec: IntentSpec, original: str) -> dict[str, str]:
        """Извлекает слоты из текста «как сказал пользователь».

        Тот же шаблон ищется в исходной строке. Это единственный способ
        сохранить «ё» в именах файлов: нормализация нужна для сопоставления,
        но ломает имена.
        """
        source = (original or "").strip().lower()
        for pattern in spec._compiled:
            found = pattern.search(source)
            if found:
                return found.groupdict()
        return {}

    def parse_slots(self, spec: IntentSpec, raw: dict[str, str],
                    text: str = "") -> dict[str, Any]:
        """Превращает строки из шаблона в типизированные значения."""
        slots: dict[str, Any] = {}

        for name, value in raw.items():
            slot = spec.slots.get(name)
            if slot is None:
                continue
            if value is None or not str(value).strip():
                if slot.default is not None:
                    slots[name] = slot.default
                continue

            slots[name] = self._parse_value(slot, str(value), text)

        return slots

    def _parse_value(self, slot: SlotSpec, raw: str, text: str) -> Any:
        value = raw.strip()

        # Число в необязательном слоте: если разобрать не удалось, берём
        # значение по умолчанию, а не падаем. «сделай громче на пять» ->
        # 5, «сделай громче на абв» -> значение по умолчанию.
        if slot.type == "number" and not slot.required:
            try:
                return parse_number(value)
            except InvalidSlot:
                return slot.default

        if slot.type == "duration":
            return parse_duration(value)
        if slot.type == "number":
            return parse_number(value)
        if slot.type == "time":
            return parse_time(value)
        if slot.type == "datetime":
            return parse_datetime_slot(value)
        if slot.type == "bool":
            return value not in ("", "нет", "не", "no", "false")
        if slot.type == "path":
            # Пути и имена файлов нельзя нормализовать с заменой «ё»:
            # «отчёт.txt» и «отчет.txt» — это разные файлы.
            return cleanup(value, keep_yo=True)

        return cleanup(value)

    # --- выполнение -------------------------------------------------------

    def find_spec(self, name: str) -> IntentSpec:
        for spec in self._specs:
            if spec.name == name:
                return spec
        raise SkillNotFound(name)

    async def execute(self, match: Match, ctx) -> Response:  # noqa: ANN001
        """Вызывает обработчик навыка."""
        missing = self.has_missing_slots(match)
        if missing:
            names = ", ".join(s.name for s in missing)
            return Response(
                speech=f"Не понял, что именно: нужно {names}",
                ask_for=missing[0].name,
            )

        try:
            result = match.spec.handler(ctx, match.slots)
            if hasattr(result, "__await__"):
                return await result
            return result
        except SinapsError:
            # Ошибку, у которой есть `speech`, нельзя заворачивать.
            #
            # Раньше здесь стоял `except SkillError`, и любая другая
            # наша ошибка — в том числе `PluginError` из дочернего
            # процесса — превращалась в «Что-то сломалось, попробуй
            # ещё раз». При этом `speech` через границу процесса мы
            # переносили специально, и `api.py` прямо велит автору
            # плагина бросать ошибки из витрины. Работать это могло
            # только наполовину: объяснение доезжало и тут же
            # выбрасывалось.
            #
            # Всё остальное — настоящие поломки, не связанные с
            # контрактом — по-прежнему заворачивается.
            raise
        except Exception as exc:
            log.exception("Навык '%s' упал", match.spec.name)
            raise SkillError(
                f"Навык {match.spec.name} завершился ошибкой",
                speech="Что-то сломалось, попробуй ещё раз",
            ) from exc

    # --- справка ----------------------------------------------------------

    def capabilities(self) -> list[dict[str, Any]]:
        """Машиночитаемый список возможностей — для LLM и для «что умеешь»."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "examples": spec.examples,
                "dangerous": spec.dangerous,
            }
            for spec in self._specs
            if not spec.hidden
        ]

    def help_text(self, *, group_by_skill: bool = True) -> str:
        """Человекочитаемый список команд."""
        if not group_by_skill:
            return "\n".join(
                f"  {spec.name} — {spec.description}"
                for spec in self._specs if not spec.hidden
            )

        lines: list[str] = []
        for skill in self._skills:
            visible = [s for s in skill.intents if not s.hidden]
            if not visible:
                continue
            lines.append(f"\n{_title(skill.name)}:")
            for spec in visible:
                example = spec.examples[0] if spec.examples else spec.description
                mark = "  (нужно подтверждение)" if spec.dangerous else ""
                lines.append(f"  • {example}{mark}")
        return "\n".join(lines).strip()

    def reference_text(self, *, with_patterns: bool = True) -> str:
        """Полный справочник команд для файла.

        В отличие от `help_text` содержит:
          * полное техническое имя (для отладки и для LLM);
          * ВСЕ реальные варианты фразы, а не один пример;
          * слоты с типами и описаниями;
          * признак опасности и требование подтверждения.

        Файл собирается из реестра, поэтому не может разойтись с реальностью.
        """
        total = len(self._specs)
        dangerous = sum(1 for s in self._specs if s.dangerous)

        out: list[str] = [
            "=" * 78,
            "  SINAPS — СПРАВОЧНИК КОМАНД",
            "=" * 78,
            "",
            f"  Всего команд: {total}    навыков: {len(self._skills)}    "
            f"опасных: {dangerous}",
            "",
            "  ЭТОТ ФАЙЛ СОЗДАЁТСЯ АВТОМАТИЧЕСКИ из реестра навыков.",
            "  Перегенерировать:  python -m sinaps commands --out docs\\COMMANDS.md",
            "",
            "  Полное имя (intent) — техническое название, по нему можно",
            "  проверить срабатывание:  python -m sinaps try \"фраза\"",
            "",
            "=" * 78,
        ]

        for skill in self._skills:
            visible = [s for s in skill.intents if not s.hidden]
            if not visible:
                continue

            out.append("")
            out.append(f"  {skill.name.upper()} — {skill.description}")
            out.append("  " + "-" * 74)

            for spec in sorted(visible, key=lambda s: s.name):
                mark = "  [ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ]" if spec.dangerous else ""
                out.append("")
                out.append(f"    ПОЛНОЕ ИМЯ: {spec.name}{mark}")
                out.append(f"    Что делает: {spec.description}")

                phrases = self._phrases_of(spec)
                out.append("    Что можно сказать:")
                for phrase in phrases:
                    out.append(f"      - «{phrase}»")

                if spec.slots:
                    out.append("    Слоты (что нужно извлечь из фразы):")
                    for slot in spec.slots.values():
                        required = "обязателен" if slot.required else "необязателен"
                        desc = f" — {slot.description}" if slot.description else ""
                        out.append(f"      - {slot.name} ({slot.type}, {required}){desc}")

                if with_patterns and spec.patterns:
                    out.append("    Шаблоны распознавания (служебное):")
                    for pattern in spec.patterns:
                        out.append(f"      {pattern}")

        out.append("")
        out.append("=" * 78)
        out.append("  Команды в трее и консоли:")
        out.append("    /help        справка")
        out.append("    /devices     аудиоустройства")
        out.append("    /config      текущая конфигурация")
        out.append("    /metrics     задержки")
        out.append("    /history     история диалогов")
        out.append("    /reload      перечитать config.yaml")
        out.append("    /quiet on|off  включить/выключить озвучку")
        out.append("    /quit        выход")
        out.append("=" * 78)

        return "\n".join(out)

    @staticmethod
    def _phrases_of(spec: IntentSpec) -> list[str]:
        """Реальные варианты фразы для раздела «Что можно сказать».

        Берутся ТОЛЬКО примеры, объявленные автором команды: они гарантированно
        рабочие и читаются как обычный текст.

        Автоматически переводить шаблоны регулярных выражений в прозу нельзя:
        выходит мусор вроде «какой|который час|время \\s*?». Шаблоны остаются
        в отдельном служебном разделе, где им и место.
        """
        phrases = [p.strip() for p in spec.examples if p.strip()]
        if phrases:
            return phrases

        # Примеров нет — показываем хотя бы что-то читаемое.
        for pattern in spec.patterns:
            readable = _pattern_to_phrase(pattern)
            if readable:
                phrases.append(readable)
        return phrases


#: Замены при переводе регулярного выражения в читаемую фразу.
_PATTERN_PHRASES = (
    (r"\(\?:", "("),
    (r"\(", "("),
    (r"\?\)", " или "),
    (r"\[её\]", "е/ё"),
    (r"\w\*", ""),
    (r"\w\+", ""),
    (r"\s+", " "),
    (r"\*", " "),
    (r"\.", ""),
    (r"\?", ""),
    (r"[", ""),
    (r"]", ""),
    (r"\\", ""),
    (r"^", ""),
    (r"$", ""),
)


def _pattern_to_phrase(pattern: str) -> str:
    """Переводит шаблон в вид, близкий к произносимой фразе.

    Не идеально, но достаточно для справочника: пользователю нужно понять
    порядок слов и понятные варианты, а не точная грамматика движка.
    """
    text = pattern
    # Именованные группы -> содержимое группы.
    text = re.sub(r"\(\?P<[^>]+>", "(", text)
    # Неименованные группы -> содержимое.
    text = re.sub(r"\(\?:", "(", text)

    for src, dst in _PATTERN_PHRASES:
        text = text.replace(src, dst)

    text = re.sub(r"[\s()]+", " ", text)
    text = re.sub(r"\s*или\s*", " или ", text)
    text = re.sub(r"\s*или\s*или\s*", " или ", text)
    text = text.strip(" |/,-")
    text = re.sub(r"\s{2,}", " ", text)

    # Слипшиеся куски («делайгромче») разбиваем по строчным после заглавной.
    text = re.sub(r"(?<=[а-яё])(?=[А-ЯЁ])", " ", text)
    return text.strip()


_RU_TITLES = {
    "time": "Время и дата",
    "apps": "Программы и файлы",
    "media": "Звук и мультимедиа",
    "system_info": "Состояние системы",
    "power": "Питание",
    "screen": "Экран",
    "clipboard": "Буфер обмена",
    "windows": "Окна",
    "smalltalk": "Общение",
    "help": "Справка",
}


def _title(skill_name: str) -> str:
    return _RU_TITLES.get(skill_name, skill_name.replace("_", " ").capitalize())
