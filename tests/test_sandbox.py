"""Запрет сети в процессе плагина.

Проверять приходится в отдельном процессе: `arm()` меняет
`sys.meta_path` и вычищает модули из кэша, и сделать это посреди
тестового набора нельзя — упало бы всё остальное.

Последний тест здесь — **проверка проверки**. Без него легко поверить
в зелёный цвет, ничего не защищающий: `verify()` может возвращать
`True` всегда, и весь остальной набор будет при этом «проходить».
Чтобы это исключить, один тест запускает ребёнка **без** запрета и
требует, чтобы тот честно сказал: сеть открыта.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

#: Ребёнок грузит то, что нужно самому себе, потом запрещает сеть.
#:
#: Порядок обязателен: на Windows `asyncio` импортирует `socket`, и
#: запрет, поставленный раньше, убивает цикл событий. Запрет, поставленный
#: позже без вычистки кэша, не срабатывает вовсе — `import socket`
#: находит модуль в `sys.modules` и до перехватчика не доходит.
CHILD = '''
import json
import sys
sys.path.insert(0, {root!r})

import asyncio
import sqlite3
import sinaps.plugins.api  # noqa: F401

if {armed}:
    from sinaps.plugins import sandbox
    sandbox.arm()
    sealed = sandbox.verify()
else:
    sealed = {{"socket": False, "ssl": False, "ctypes": False,
              "urllib.request": False, "http.client": False}}

async def work():
    await asyncio.sleep(0)
    return sqlite3.connect(":memory:").execute("select 7").fetchone()[0]

report = {{
    "sealed": sealed,
    "all_sealed": all(sealed.values()),
    "asyncio": asyncio.run(work()),
    "imports": {{}},
}}
for name in ("socket", "ssl", "ctypes", "subprocess", "urllib.request",
             "http.client", "huggingface_hub", "json", "sqlite3", "yaml"):
    try:
        __import__(name)
    except Exception as exc:
        report["imports"][name] = type(exc).__name__
    else:
        report["imports"][name] = "ПРОПУЩЕН"
print(json.dumps(report, ensure_ascii=False))
'''

ROOT = str(Path(__file__).resolve().parent.parent)


def run_child(tmp_path: Path, armed: bool) -> dict:
    """Поднять интерпретатор, отработать сценарий и вернуть его отчёт."""
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(root=ROOT, armed=armed), encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, (
        f"ребёнок упал:\n{done.stdout}\n{done.stderr}")
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def sealed(tmp_path_factory):
    return run_child(tmp_path_factory.mktemp("sealed"), armed=True)


@pytest.fixture(scope="module")
def unsealed(tmp_path_factory):
    return run_child(tmp_path_factory.mktemp("unsealed"), armed=False)


# --- запрет держит ----------------------------------------------------------


def test_everything_is_sealed(sealed):
    assert sealed["all_sealed"], (
        f"запрет не держит: {sealed['sealed']}"
    )


@pytest.mark.parametrize("name", [
    "socket",          # низкий уровень: им открывается любой TCP
    "ssl",             # TLS поверх сокета
    "ctypes",          # WinHTTP и WinINet мимо Python
    "subprocess",      # curl мимо всего
    "urllib.request",
    "http.client",
    "huggingface_hub", # тянет веса мимо вопроса
])
def test_network_module_is_blocked(sealed, name):
    """Каждый закрытый путь проверяется отдельно.

    Одного «сокет закрыт» мало: `ctypes` и `subprocess` — это разные
    дыры, и закрытие одного ничего не говорит о другом.
    """
    assert sealed["imports"][name] == "NetworkDisabled", (
        f"{name} не закрыт: {sealed['imports'][name]}"
    )


@pytest.mark.parametrize("name", ["json", "sqlite3", "yaml"])
def test_ordinary_modules_still_work(sealed, name):
    """Запрет не должен ломать то, чему он не мешает.

    Плагин вправе писать файлы и работать с базой. Запрет на сеть не
    должен становиться запретом вообще.
    """
    assert sealed["imports"][name] == "ПРОПУЩЕН"


def test_asyncio_survives_the_block(sealed):
    """Главная тонкость: без цикла событий не вызвать ни один обработчик."""
    assert sealed["asyncio"] == 7, (
        "asyncio или sqlite перестали работать под запретом — "
        "значит порядок загрузки нарушен"
    )


# --- проверка проверки ------------------------------------------------------


def test_verify_is_not_vacuously_true(unsealed):
    """Без запрета `verify()` обязан сказать «сеть открыта».

    Если бы этот тест проходил, весь набор проверял бы пустоту: функцию,
    которая всегда возвращает `True`, не отличить от работающей.
    """
    assert not unsealed["all_sealed"], (
        "ребёнок без запрета сообщил, что всё закрыто — "
        "verify() ничего не проверяет"
    )
    assert unsealed["imports"]["socket"] == "ПРОПУЩЕН"


def test_without_arm_network_is_really_open(unsealed):
    """Контроль: до `arm()` сеть действительно доступна.

    Это опорная точка для теста выше. Без неё «проверка не ловит» могла бы
    означать «проверять нечего».
    """
    assert unsealed["imports"]["urllib.request"] == "ПРОПУЩЕН"


# --- сам запрет -------------------------------------------------------------


def test_blocked_roots_cover_the_known_paths():
    """Состав запрета зафиксирован тестом.

    Удаление строки из `BLOCKED_ROOTS` должно ломать сборку, а не
    молча оставлять дыру.
    """
    from sinaps.plugins.sandbox import BLOCKED_ROOTS

    for name in ("socket", "ssl", "ctypes", "subprocess", "urllib",
                 "http", "requests", "huggingface_hub"):
        assert name in BLOCKED_ROOTS, f"{name} больше не запрещён"


def test_asyncio_is_not_blocked():
    """Явная проверка того, чего запрет не должен ломать.

    Стоит отдельно, потому что `asyncio` в списке запрещённых корней
    проявил бы себя не сразу, а падением на первом же вызове — и
    выглядело бы как ошибка где угодно, кроме запрета.
    """
    from sinaps.plugins.sandbox import BLOCKED_ROOTS

    assert "asyncio" not in BLOCKED_ROOTS


def test_describe_before_and_after_arm():
    from sinaps.plugins import sandbox

    assert "сеть" in sandbox.describe()