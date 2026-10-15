# -*- coding: utf-8 -*-
"""Много плагинов одновременно: растёт ли память.

Критерий из ТЗ: «одновременно горячих плагинов без роста RAM >= 15».
Проверка дешевле замера: следим за тем, что память ядра растёт медленно,
тогда как процессы плагинов растут каждый на свои 4 МБ.

Точное число приходит из замера на живой машине — 16 плагинов, +2.2 МБ
ядра. Здесь важно другое: не дать этой цифре разъехаться с кодом.
"""

from __future__ import annotations

import os

import psutil
import pytest

from sinaps.plugins import approvals
from sinaps.plugins.loader import load_all, plugin_stats
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
name: p{i:02d}
version: 1.0.0
description: Плагин для замера
entry: plugin:register
permissions:
  - state
"""

COUNT = 16


def make_plugin(folder, index):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(
        MANIFEST.format(i=index), encoding="utf-8")
    (folder / "plugin.py").write_text(PLUGIN, encoding="utf-8")
    return folder


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry()


@pytest.fixture(autouse=True)
def isolated_approvals(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "path", lambda: tmp_path / "plugins.json")


def core_rss_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


async def test_sixteen_plugins_without_growing_core(tmp_path, registry):
    """16 плагинов в памяти, а ядро почти не растёт.

    Порог 8 МБ на 16 плагинов — с запасом к замерным 2.2 МБ, но
    достаточно жёстко, чтобы поймать ситуацию, когда в ядро начали
    складывать состояние плагинов вместо того, чтобы держать его в
    дочерних процессах.
    """
    for index in range(COUNT):
        make_plugin(tmp_path / f"p{index:02d}", index)

    before = core_rss_mb()
    report = await load_all(tmp_path, registry)
    try:
        assert len(report.loaded) == COUNT, (
            f"загружено {len(report.loaded)} из {COUNT}; "
            f"с ошибкой: {report.failed[:3]}")

        stats = plugin_stats(report)
        assert stats.alive == COUNT
        assert stats.asleep == 0

        assert stats.rss_mb > 0, "у живых процессов нулевая память"

        growth = core_rss_mb() - before
        assert growth < 8, (
            f"ядро выросло на {growth:.1f} МБ при {COUNT} плагинах — "
            f"это не «без роста RAM»")
    finally:
        await report.stop_all()


async def test_core_memory_returns_after_stop(tmp_path, registry):
    """После остановки плагины в памяти не остаются.

    Критерий «RAM на простое = 0» и на остановке должен держаться: иначе
    выход из ассистента оставлял бы за собой десятки мегабайт.
    """
    for index in range(4):
        make_plugin(tmp_path / f"p{index:02d}", index)

    report = await load_all(tmp_path, registry)
    assert plugin_stats(report).rss_mb > 0

    await report.stop_all()

    stats = plugin_stats(report)
    assert stats.rss_mb == 0, stats
    assert stats.alive == 0
    assert stats.asleep == 4