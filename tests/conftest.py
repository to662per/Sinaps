"""Общие фикстуры тестов.

Заглушка платформы живёт не здесь, а в пакете: `FakePlatform` —
часть публичной обвязки для проверки плагинов
(`sinaps.plugins.testing`), и автору плагина она нужна не меньше, чем
нам. Держать вторую копию в `conftest.py` значило бы через месяц
обнаружить, что в заглушке для наших тестов есть `volume_set`, а в
заглушке для чужих — нет, и что-то одно из них проверяет саму себя.

По той же причине импорт делается из пакета, а не из `tests.xxx`:
без `tests/__init__.py` прямой импорт тест-модуля приводит к
неоднозначности и зависаниям при совместном запуске.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sinaps.plugins.testing import FakePlatform


@pytest.fixture(autouse=True)
def _no_writes_to_real_config(tmp_path_factory, monkeypatch):
    """Запретить тестам писать в боевой `data/config.yaml`.

    Стояло бы на словах — но слова оказываются слабее кода: тест на
    правку настройки однажды оставил в конфигурации пользователя
    скорость голоса 1.3 вместо 1.0, и заметил это только при
    `git status`. Теперь запись в настоящий файл роняет тест, а не
    проходит тихо.
    """
    from sinaps.core import config as config_module

    real = config_module.CONFIG_PATH.resolve()
    guard = tmp_path_factory.mktemp("cfg") / "config.yaml"
    original = Path.write_text

    def _guarded(self, *args, **kwargs):
        # Защита стоит на самой записи на диск, а не на
        # `save_config`: модули импортируют её прямой ссылкой
        # (`from ..core.config import save_config`), и подмена
        # атрибута модуля их не касается. Подмена функции нашла бы
        # себя только там, где импорта не было.
        try:
            if self.resolve() == real:
                raise AssertionError(
                    f"тест пишет в боевую конфигурацию {real}: "
                    "укажи свой файл из tmp_path и передай его путь")
        except (OSError, ValueError):
            pass
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _guarded)
    return guard


@pytest.fixture
def platform_stub() -> FakePlatform:
    """Свежая заглушка на каждый тест: список вызовов не протекает."""
    return FakePlatform()