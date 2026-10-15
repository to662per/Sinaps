# -*- coding: utf-8 -*-
"""Снимок плагинов для интерфейса: сборка из отчёта о загрузке.

Здесь нет Qt намеренно. Снимок должен собираться и проверяться без
приложения Qt: интерфейс — один из потребителей, а не единственный.
Плюс так тесты идут быстро и не падают в CI без дисплея.
"""

from __future__ import annotations

import pytest

from sinaps.plugins.loader import load_all
from sinaps.plugins.view import (STATUS_ASLEEP, STATUS_DISABLED,
                                 STATUS_ERROR, STATUS_LOADED,
                                 build_plugin_view)
from sinaps.skills.registry import SkillRegistry

PLUGIN = '''\
from sinaps.plugins.api import Response, Skill

async def handle(ctx, slots):
    return Response(speech="услышал")

def register(registry):
    skill = Skill("get")
    skill.command("now", handle, patterns=[r"^погода$"],
                  examples=["погода"], slots={})
    registry.add(skill)
    return skill
'''

MANIFEST = """\
name: {name}
version: 1.0.0
description: Плагин для проверки
entry: plugin:register
permissions:
{permissions}
"""


def make_plugin(folder, name="weather", permissions="  - state"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(
        MANIFEST.format(name=name, permissions=permissions),
        encoding="utf-8")
    (folder / "plugin.py").write_text(PLUGIN, encoding="utf-8")
    return folder


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry()


@pytest.fixture(autouse=True)
def isolated_approvals(tmp_path, monkeypatch):
    from sinaps.plugins import approvals

    monkeypatch.setattr(approvals, "path", lambda: tmp_path / "plugins.json")


# --- карточка ----------------------------------------------------------------


async def test_loaded_plugin_becomes_card(tmp_path, registry):
    """Загруженный плагин — карточка со статусом «активен»."""
    make_plugin(tmp_path / "a", name="a")
    report = await load_all(tmp_path, registry)
    try:
        view = build_plugin_view(report)
        assert len(view.cards) == 1

        card = view.cards[0]
        assert card.name == "a"
        assert card.status == STATUS_LOADED
        assert card.status_title == "активен"
        assert card.commands == ("a.get.now",)
    finally:
        await report.stop_all()


async def test_failed_plugin_becomes_card_with_reason(tmp_path, registry):
    """Незагруженный плагин виден с причиной, а не исчезает.

    Иначе человек увидит «плагин не работает» и не будет знать почему.
    """
    folder = make_plugin(tmp_path / "bad", name="bad")
    (folder / "plugin.py").write_text("import нет_такого\n", encoding="utf-8")

    report = await load_all(tmp_path, registry)
    try:
        view = build_plugin_view(report)

        assert view.failed == 1
        bad = [c for c in view.cards if c.status == STATUS_ERROR]
        assert len(bad) == 1
        assert bad[0].error, "у сломанного плагина нет причины"
        assert bad[0].can_reload is False
    finally:
        await report.stop_all()


async def test_sleeping_plugin_after_reap(tmp_path, registry):
    """После уборки плагин показан как спящий, а не как активный.

    Если бы статус считался по наличию плагина в отчёте, спящий плагин
    выглядел бы рабочим — а он не отвечает.
    """
    from sinaps.plugins.loader import reap_idle

    make_plugin(tmp_path / "a", name="a")
    report = await load_all(tmp_path, registry)
    try:
        await reap_idle(report, 0)

        view = build_plugin_view(report)
        assert view.cards[0].status == STATUS_ASLEEP
        assert view.cards[0].status_title == "спит"
        assert view.cards[0].memory_label == "—"
    finally:
        await report.stop_all()


async def test_disabled_plugin_marked_and_not_reloadable(tmp_path, registry):
    """Выключенный плагин помечен и не перезагружается.

    Его команды убраны из реестра намеренно: кнопка «Перезагрузить»
    вернула бы то, что человек отключил.
    """
    make_plugin(tmp_path / "a", name="a")
    report = await load_all(tmp_path, registry)
    try:
        view = build_plugin_view(report, disabled=frozenset({"a"}))

        card = view.cards[0]
        assert card.status == STATUS_DISABLED
        assert card.can_reload is False
    finally:
        await report.stop_all()


# --- права -------------------------------------------------------------------


async def test_permission_granted_state(tmp_path, registry):
    """У плагина с одобренным правом оно показано как одобренное."""
    from sinaps.plugins import approvals

    make_plugin(tmp_path / "a", name="a", permissions="  - state")
    approvals.grant("a", ["state"])

    report = await load_all(tmp_path, registry)
    try:
        view = build_plugin_view(report)
        card = view.cards[0]

        assert len(card.permissions) == 1
        assert card.permissions[0].granted is True
        assert card.needs_approval is False
    finally:
        await report.stop_all()


async def test_permission_without_approval_offers_it(tmp_path, registry):
    """Без одобрения плагин показывает кнопку."""
    make_plugin(tmp_path / "a", name="a", permissions="  - network.http")

    report = await load_all(tmp_path, registry)
    try:
        card = build_plugin_view(report).cards[0]

        assert card.permissions[0].granted is False
        assert card.needs_approval is True
    finally:
        await report.stop_all()


async def test_permission_title_is_human(tmp_path, registry):
    """В карточке — человеческое объяснение, а не имя права."""
    make_plugin(tmp_path / "a", name="a", permissions="  - network.http")

    report = await load_all(tmp_path, registry)
    try:
        permission = build_plugin_view(report).cards[0].permissions[0]

        assert permission.title == "Отправлять запросы в интернет"
        assert permission.name == "network.http"
        assert "Одобрить" in permission.label or "одобрить" in permission.label
    finally:
        await report.stop_all()


# --- память ------------------------------------------------------------------


async def test_memory_only_for_live_processes(tmp_path, registry):
    """Память суммируется только по живым процессам."""
    from sinaps.plugins.loader import reap_idle

    for name in ("a", "b"):
        make_plugin(tmp_path / name, name=name)

    report = await load_all(tmp_path, registry)
    try:
        before = build_plugin_view(report)
        assert before.total_rss_mb > 0
        assert all(c.rss_mb > 0 for c in before.cards)

        await reap_idle(report, 0)

        after = build_plugin_view(report)
        assert after.total_rss_mb == 0, (
            "спящие плагины продолжают занимать память в снимке")
        assert all(c.rss_mb == 0 for c in after.cards)
    finally:
        await report.stop_all()


async def test_empty_report_makes_empty_view(tmp_path, registry):
    """Нет плагинов — пустой снимок, а не исключение."""
    view = build_plugin_view(await load_all(tmp_path, registry))

    assert view.cards == []
    assert view.total == 0
    assert "не загружены" in view.summary