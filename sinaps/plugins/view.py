# -*- coding: utf-8 -*-
"""Данные о плагинах для интерфейса.

Отдельный модуль и без Qt: окно живёт в Qt-потоке, а плагины — в
asyncio-потоке, и отдавать первому ссылку на второе нельзя. Здесь
собирается простой снимок, который можно спокойно читать из виджета.

Снимок, а не живая ссылка — по двум причинам. Первая та же, что и у
панели возможностей: пересечение потоков. Вторая — карточка плагина
показывает счётчик, и он обязан совпадать с журналом. Отдельный
счётчик в интерфейсе со временем разошёлся бы с тем, что пишет в лог,
а расхождение в метриках не замечает никто.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Статус плагина для карточки.
STATUS_LOADED = "loaded"      # процесс жив, ждёт вызова
STATUS_ASLEEP = "asleep"      # процесс закрыт по простоям
STATUS_ERROR = "error"        # не загрузился, есть причина
STATUS_DISABLED = "disabled"  # выключен пользователем

#: Человеческие подписи. Ключ — состояние сессии, значение — текст.
STATUS_TITLES = {
    STATUS_LOADED: "активен",
    STATUS_ASLEEP: "спит",
    STATUS_ERROR: "ошибка",
    STATUS_DISABLED: "выключен",
}


@dataclass(frozen=True)
class PermissionView:
    """Одно право плагина: что просит и что выдано."""

    name: str
    #: Пояснение по-русски. Без него в списке видны строки вида
    #: `platform.power`, и человек не понимает, о чём речь.
    title: str
    granted: bool

    @property
    def label(self) -> str:
        return f"{self.title} — {'одобрено' if self.granted else 'нужно одобрить'}"


@dataclass(frozen=True)
class PluginCard:
    """Карточка плагина для окна."""

    name: str
    version: str
    description: str
    status: str
    #: Мегабайты. Для спящего плагина ноль: процесса уже нет, и
    #: показывать его прошлую память значит врать.
    rss_mb: float
    commands: tuple[str, ...]
    permissions: tuple[PermissionView, ...] = ()
    #: Последняя ошибка загрузки. Пусто у живого плагина.
    error: str = ""
    #: Можно ли перезагрузить. Выключенный плагин перезагрузку не
    #: переживёт: его команда удалена из реестра намеренно.
    can_reload: bool = True

    @property
    def status_title(self) -> str:
        return STATUS_TITLES.get(self.status, self.status)

    @property
    def needs_approval(self) -> bool:
        """Есть ли право, которого не хватает."""
        return any(not p.granted for p in self.permissions)

    @property
    def memory_label(self) -> str:
        if self.status != STATUS_LOADED:
            return "—"
        return f"{self.rss_mb:.1f} МБ"


def build_plugin_view(report, disabled: frozenset[str] = frozenset(),
                      failed: list[tuple[str, str]] | None = None) -> PluginView:
    """Собрать снимок из отчёта о загрузке.

    `disabled` — имена плагинов, выключенных пользователем. Такой плагин
    показывается как выключенный и не перезагружается: его команды
    убраны из реестра намеренно, и перезагрузка вернула бы то, что
    человек отключил.

    `failed` по умолчанию берётся из самого отчёта. Раньше список надо
    было передавать отдельно, и вызов без него тихо показывал ноль
    ошибок: отчёт об ошибках есть, а на экране — нет.
    """
    from .manifest import permission_title
    from .loader import plugin_stats

    failed = report.failed if failed is None else failed

    rss_by_name: dict[str, float] = {}
    total_rss = plugin_stats(report).rss_mb
    alive = sum(1 for p in report.loaded
                if p.process is not None and p.process.alive)

    # Память делим между живыми поровну: у каждого процесса своя, но
    # точное значение по плагину снимок не несёт, и делить честнее, чем
    # показывать общую сумму в каждой карточке.
    per_plugin = (total_rss / alive) if alive else 0.0
    for plugin in report.loaded:
        rss_by_name[plugin.name] = per_plugin

    errors = dict(failed or [])

    cards: list[PluginCard] = []
    for plugin in report.loaded:
        if plugin.name in disabled:
            status = STATUS_DISABLED
        elif plugin.process is not None and plugin.process.alive:
            status = STATUS_LOADED
        else:
            status = STATUS_ASLEEP

        permissions = tuple(
            PermissionView(
                name=name,
                title=permission_title(name),
                granted=name in plugin.granted,
            )
            for name in sorted(plugin.permissions)
        )

        cards.append(PluginCard(
            name=plugin.name,
            version=plugin.manifest.version,
            description=plugin.manifest.description,
            status=status,
            rss_mb=rss_by_name.get(plugin.name, 0.0)
            if status == STATUS_LOADED else 0.0,
            commands=plugin.commands,
            permissions=permissions,
            error=errors.get(plugin.name, ""),
            can_reload=status != STATUS_DISABLED,
        ))

    for folder_name, reason in (failed or []):
        name = folder_name
        if name in {c.name for c in cards}:
            continue
        cards.append(PluginCard(
            name=name,
            version="—",
            description="не загружен",
            status=STATUS_ERROR,
            rss_mb=0.0,
            commands=(),
            error=reason,
            can_reload=False,
        ))

    return PluginView(
        cards=cards,
        total=len(report.loaded) + len(failed or []),
        # Считаем по карточкам, а не по аргументу: в списке отчёта папка
        # названа именем папки, а в карточке — именем из манифеста. Если
        # они разойдутся, плагин не был бы добавлен вовсе, и счётчик
        # ошибок показывал бы больше, чем карточек с ошибкой.
        failed=sum(1 for c in cards if c.status == STATUS_ERROR),
    )


@dataclass
class PluginView:
    """Снимок состояния всех плагинов."""

    cards: list[PluginCard] = field(default_factory=list)
    #: Сколько всего плагинов и сколько из них не загрузилось.
    total: int = 0
    failed: int = 0

    @property
    def summary(self) -> str:
        """Строка для заголовка окна."""
        if not self.total:
            return "Плагины не загружены"
        parts = [f"всего {self.total}"]
        alive = sum(1 for c in self.cards if c.status == STATUS_LOADED)
        asleep = sum(1 for c in self.cards if c.status == STATUS_ASLEEP)
        if alive:
            parts.append(f"активных {alive}")
        if asleep:
            parts.append(f"спящих {asleep}")
        if self.failed:
            parts.append(f"с ошибкой {self.failed}")
        return ", ".join(parts)

    @property
    def total_rss_mb(self) -> float:
        return sum(c.rss_mb for c in self.cards)