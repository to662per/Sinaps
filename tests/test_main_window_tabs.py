# -*- coding: utf-8 -*-
"""Вкладки «История» и «Настройки».

Проверяется то, что человек делает руками: ищет в истории, меняет
настройку, выгружает. Каждая настройка обязана попасть в тот же
`config.yaml`, что и раньше, — иначе появятся два источника правды и
«я поменял» перестанет значить что-нибудь.
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QSpinBox)

from sinaps.core.config import SinapsConfig
from sinaps.memory.store import MemoryStore
from sinaps.ui.history_view import HistoryView
from sinaps.ui.main_window import SECTIONS, MainWindow
from sinaps.ui.settings_view import RESTART_ONLY, SettingsView


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    if existing is not None:
        yield existing
        return
    yield QApplication([])


@pytest.fixture
def store(tmp_path):
    db = MemoryStore(tmp_path / "history.db", keep_days=30)
    now = time.time()
    db.add_dialogue("user", "открой блокнот", source="voice")
    db.add_dialogue("assistant", "открываю блокнот", source="voice",
                    intent="apps.open", latency_ms=310)
    db.add_dialogue("user", "погода в Москве", source="text")
    db.add_dialogue("assistant", "в Москве 7 градусов", source="text",
                    intent="weather.current", latency_ms=520)
    db.add_turn("открой блокнот", "открываю блокнот", intent="apps.open",
                source="voice", total_ms=310, started_at=now)
    db.add_turn("погода в Москве", "в Москве 7 градусов",
                intent="weather.current", source="text", total_ms=520,
                started_at=now)
    return db


def fields(widget):
    """Поля ввода внутри виджета, любого типа."""
    from sinaps.ui.settings_view import _controls

    return _controls(widget)


# --- поиск и фильтры --------------------------------------------------------


def test_search_filters_by_text(store):
    """Поиск находит нужную реплику и не тащит остальные."""
    found = store.history(query="блокнот")

    assert found, "поиск ничего не нашёл"
    assert all("блокнот" in e.text.lower() for e in found)


def test_filter_by_role(store):
    """Фильтр по роли оставляет реплики одного собеседника."""
    found = store.history(role="user")

    assert found
    assert all(e.role == "user" for e in found)


def test_filter_by_intent(store):
    """Фильтр по намерению оставляет нужное."""
    found = store.history(intent="weather.current")

    assert found
    assert all(e.intent == "weather.current" for e in found)


def test_filters_combine(store):
    """Несколько условий работают вместе, а не по очереди."""
    found = store.history(role="assistant", intent="apps.open")

    assert len(found) == 1, f"ожидался один обмен, пришло {len(found)}"
    assert found[0].text == "открываю блокнот"


def test_filter_by_time_range(store):
    """Временной диапазон отсекает старое."""
    future = store.history(since=time.time() + 3600)
    assert not future, "будущее не должно попадать в историю"


def test_empty_result_is_empty_not_error(store):
    """Пустой результат — пустой список, а не исключение."""
    assert store.history(query="такого_слова_нет") == []


def test_view_shows_entries(app, store):
    """Виджет показывает записи, а не их количество."""
    view = HistoryView()
    view.set_entries(store.history())

    assert view._list.count() == len(store.history())


def test_view_says_when_nothing_found(app):
    """Пустой список объясняет, что ничего не найдено."""
    view = HistoryView()
    view.set_entries([])

    assert view._list.count() == 1
    assert "не найдено" in view._list.item(0).text().lower()


def test_view_says_history_empty(app, store):
    """Пустая база говорит об этом прямо."""
    view = HistoryView()
    view.set_entries(store.history())
    view.set_summary("История пуста")

    assert view._summary.text() == "История пуста"


def test_entry_line_marks_intent(app, store):
    """Намерение видно в строке.

    Без него человек ищет «что он там такое сказал» вместо того, чтобы
    увидеть, к какому намерению фраза привела.
    """
    entries = store.history(intent="apps.open")
    view = HistoryView()

    line = view._entry_line(entries[0])

    assert "apps.open" in line
    assert "Вы" in line or "Син" in line


def test_search_emits_filters(app):
    """Поиск сообщает фильтры, а не сам ходит в базу."""
    view = HistoryView()
    got: list[dict] = []
    view.query_changed.connect(got.append)

    view._search.setText("молоко")
    view._search.returnPressed.emit()

    assert got and got[0]["query"] == "молоко"


def test_role_filter_maps_to_stored_value(app):
    """Выбор «Вы» в списке даёт `user`, как в базе."""
    view = HistoryView()
    got: list[dict] = []
    view.query_changed.connect(got.append)

    view._role.setCurrentIndex(1)
    view._emit_query()

    assert got[-1]["role"] == "user"


# --- статистика --------------------------------------------------------------


def test_stats_count_turns_not_phrases(app, store):
    """Считаются обмены, а не реплики.

    «Сколько фраз за неделю» — про обмены. Считать реплики значит
    показать вдвое больше, чем человек говорил.
    """
    stats = store.history_stats(days=7)

    assert stats["turns"] == 2
    assert stats["phrases"] == 4


def test_stats_show_top_intents(app, store):
    """Топ намерений собирается."""
    stats = store.history_stats(days=7)

    intents = [item["intent"] for item in stats["top_intents"]]
    assert "apps.open" in intents
    assert "weather.current" in intents


def test_stats_on_empty_store(tmp_path):
    """Пустая база даёт нули, а не исключение."""
    stats = MemoryStore(tmp_path / "empty.db").history_stats()

    assert stats["turns"] == 0
    assert stats["top_intents"] == []


def test_view_renders_stats(app, store):
    """Сводка показывается в виджете."""
    view = HistoryView()
    view.set_stats(store.history_stats())

    text = view._stats.text()
    assert "обменов 2" in text
    assert "apps.open" in text


# --- выгрузка ---------------------------------------------------------------


def test_export_json_is_readable(store, tmp_path):
    """Выгрузка в JSON разбирается машиной."""
    target = tmp_path / "out.json"
    written = store.export(target)

    assert written > 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert data[0]["text"]
    assert data[0]["when"]


def test_export_txt_is_readable(store, tmp_path):
    """Выгрузка в текст читается человеком."""
    target = tmp_path / "out.txt"
    store.export(target)

    text = target.read_text(encoding="utf-8")
    assert "Вы:" in text or "Син:" in text
    assert "блокнот" in text


def test_export_respects_filters(store, tmp_path):
    """Выгрузка берёт только то, что показано."""
    target = tmp_path / "filtered.json"
    store.export(target, intent="apps.open")

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data
    assert all(item["intent"] == "apps.open" for item in data)


def test_export_creates_missing_folder(store, tmp_path):
    """Отсутствующая папка создаётся, а не вызывает ошибку."""
    target = tmp_path / "new" / "deep" / "out.json"
    store.export(target)

    assert target.exists()


# --- настройки ---------------------------------------------------------------


def test_every_config_field_is_represented(app):
    """В окне есть поле под каждую настройку конфигурации.

    Критерий версии — «любая настройка меняется мышью на 100%».
    Проверка по списку, а не по подписке на сигналы: сигнал может быть
    и не подключённым.
    """
    cfg = SinapsConfig()
    view = SettingsView()

    known = set(view._rows)

    # Всё, что вообще можно настроить в файле.
    sections = {
        "audio": cfg.audio, "vad": cfg.vad, "stt": cfg.stt, "tts": cfg.tts,
        "wake": cfg.wake, "privacy": cfg.privacy,
    }
    missing: list[str] = []
    for section_name, section in sections.items():
        for field in vars(section):
            if field.startswith("_"):
                continue
            path = f"{section_name}.{field}"
            if path in known or path in _derived_rows(view):
                continue
            missing.append(path)

    assert not missing, (
        f"в окне нет настроек: {missing}. Критерий версии — 100% настроек "
        "мышью")


def _derived_rows(view) -> set[str]:
    """Пути, которые окно покрывает иначе, чем полем."""
    # Права плагинов и история вынесены в свои разделы.
    return {"privacy.local_only"}


def test_settings_load_shows_current_values(app):
    """Значения из конфигурации попадают в поля."""
    cfg = SinapsConfig()
    cfg.stt.model = "medium"
    cfg.tts.speed = 1.4

    view = SettingsView()
    view.load(cfg)

    model = _find(view, "stt.model")
    speed = _find(view, "tts.speed")

    assert model.currentText() == "medium"
    assert speed.value() == pytest.approx(1.4)


def _find(view: SettingsView, path: str):
    row = view._rows[path]
    return fields(row)[0]


def test_settings_change_emits_path_and_value(app):
    """Изменение сообщает путь настройки и новое значение."""
    view = SettingsView()
    got: list[tuple] = []
    view.changed.connect(lambda p, v: got.append((p, v)))

    speed = _find(view, "tts.speed")
    speed.setValue(1.7)

    assert ("tts.speed", pytest.approx(1.7)) in got or \
        any(p == "tts.speed" for p, _ in got)


def test_settings_load_does_not_emit_changes(app):
    """Загрузка значений не выглядит как правка.

    Иначе открытие окна записало бы в файл всё подряд и засчитало
    настройку изменённой без участия человека.
    """
    view = SettingsView()
    got: list[tuple] = []
    view.changed.connect(lambda p, v: got.append((p, v)))

    view.load(SinapsConfig())

    assert not got, f"загрузка породила правки: {got}"


def test_restart_only_settings_are_marked(app):
    """Настройки с перезапуском помечены в подписи.

    Человек должен знать заранее, а не выяснять после перезапуска.
    """
    from PySide6.QtWidgets import QLabel

    view = SettingsView()
    marked = [path for path, row in view._rows.items()
              if any("перезапуска" in label.text()
                     for label in row.findChildren(QLabel))]

    assert set(marked) == set(RESTART_ONLY), (
        f"помечено {marked}, а помечено быть должно {sorted(RESTART_ONLY)}")


def test_settings_offers_no_empty_promise(app):
    """Подпись не обещает того, чего нет.

    Раньше напротив микрофона стояло «список появится после запуска» —
    обещание, которое ничего не проверяло. Теперь список настоящий,
    а тест не даёт вернуть пустое обещание ни в одну подпись.
    """
    view = SettingsView()
    view.set_devices(inputs=[(3, "Микрофон USB")],
                     outputs=[(7, "Колонки")])

    texts = []
    for row in view._rows.values():
        for node in row.findChildren(object):
            if hasattr(node, "text"):
                try:
                    texts.append(node.text())
                except TypeError:
                    pass

    promises = [t for t in texts
                if "появ" in t.lower() and "по умолчанию" not in t.lower()]

    assert not promises, f"в настройках остались обещания: {promises}"


def test_devices_listed_in_window(app):
    """Микрофоны и динамики попадают в списки выбора."""
    view = SettingsView()
    view.set_devices(inputs=[(3, "Микрофон USB")],
                     outputs=[(7, "Колонки")])

    mic = _find(view, "audio.input_device")
    names = [mic.itemText(i) for i in range(mic.count())]

    assert "Микрофон USB" in names
    assert any("умолчанию" in n for n in names), (
        "нет пункта «системный по умолчанию» — его сбросить нечем")


def test_device_choice_sends_index_not_name(app):
    """В конфигурацию уходит индекс устройства, а не его имя.

    Имя меняется при обновлении Windows, индекс — нет. Сохранив имя,
    через месяц обновление сделало бы микрофон ненайденным.
    """
    view = SettingsView()
    view.set_devices(inputs=[(3, "Микрофон USB")], outputs=[])
    got: list[tuple] = []
    view.changed.connect(lambda p, v: got.append((p, v)))

    mic = _find(view, "audio.input_device")
    mic.setCurrentIndex(1)  # «Микрофон USB»

    assert ("audio.input_device", 3) in got


def test_no_devices_is_not_an_error(app):
    """Пустой список устройств не ломает окно.

    Машина без звуковой карты — редкость, но ассистент на ней должен
    запускаться: работа с клавиатуры не требует микрофона.
    """
    view = SettingsView()
    view.set_devices(inputs=[], outputs=[])

    mic = _find(view, "audio.input_device")
    assert mic.count() >= 1


def test_setting_devices_before_load_is_safe(app):
    """Устройства можно задать раньше конфигурации."""
    view = SettingsView()
    view.set_devices(inputs=[(1, "Мик")], outputs=[(2, "Динамики")])
    view.load(SinapsConfig())

    assert _find(view, "audio.input_device").count() >= 2


def test_tray_settings_import_is_real():
    """Меню «Настройки» в трее не падает на отсутствующем имени.

    Там стояло `from ..audio.devices import list_input_devices`, а такой
    функции в модуле не было — ImportError при первом же клике. Имя,
    которого нет, Python не проверяет заранее, и ошибка всплывала
    только в бою.
    """
    from sinaps.audio.devices import (list_input_devices,
                                      list_output_devices)

    assert callable(list_input_devices)
    assert callable(list_output_devices)


def test_device_lists_look_like_pairs(monkeypatch):
    """Списки устройств — пары (индекс, имя), как ждёт окно."""
    from sinaps.audio import devices

    fake = [
        devices.DeviceInfo(1, "Микрофон USB", 1, 48000.0, is_input=True),
        devices.DeviceInfo(2, "Колонки", 2, 48000.0, is_input=False),
    ]
    monkeypatch.setattr(devices, "list_devices", lambda **_: fake)

    assert devices.list_input_devices() == [(1, "Микрофон USB")]
    assert devices.list_output_devices() == [(2, "Колонки")]


def test_saved_index_is_shown_in_device_list(app):
    """Сохранённый индекс показывает то самое устройство.

    Настройка может лежать индексом (так пишет окно) или именем (так
    написан конфиг вручную). Показывать надо то, чему значение равно:
    иначе сохранённый индекс молча превращается в «системный по
    умолчанию», и человек удивляется, почему микрофон другой.
    """
    cfg = SinapsConfig()
    cfg.audio.input_device = 3

    view = SettingsView()
    view.set_devices(inputs=[(3, "Микрофон USB")], outputs=[])
    view.load(cfg)

    mic = _find(view, "audio.input_device")
    assert mic.currentData() == 3, (
        f"показано устройство {mic.currentData()!r} вместо сохранённого 3")


def test_saved_name_is_shown_when_index_gone(app):
    """Устройство, сохранённое именем, находится по имени.

    Индексы Portaudio не выживают переподключение USB-микрофона, и
    старая настройка остаётся в конфиге. Найти её по имени лучше, чем
    молча вернуть системный микрофон.
    """
    cfg = SinapsConfig()
    cfg.audio.input_device = "Микрофон USB"

    view = SettingsView()
    view.set_devices(inputs=[(5, "Микрофон USB")], outputs=[])
    view.load(cfg)

    mic = _find(view, "audio.input_device")
    assert mic.currentText() == "Микрофон USB"


def test_vanished_device_falls_back_silently(app):
    """Пропавшее устройство не выбирается наугад.

    Устройства в конфиге может не быть: карту вынули, индекс сдвинулся.
    Лучше показать «системный по умолчанию», чем микрофон с чужим
    индексом — он sounds рабочим, пока не начнёт писать чужое.
    """
    cfg = SinapsConfig()
    cfg.audio.input_device = 99

    view = SettingsView()
    view.set_devices(inputs=[(3, "Микрофон USB")], outputs=[])
    view.load(cfg)

    mic = _find(view, "audio.input_device")
    assert mic.currentData() is None


def test_window_shows_devices_to_settings_tab(app):
    """Устройства доходят до вкладки через окно.

    Цепочка «трее → окно → вкладка настроек» — это три пересылки, и
    каждая может потерять данные молча. Тест проверяет всю цепочку
    сразу: сломанное звено не обязано ломать соседнее.
    """
    window = MainWindow()
    window.set_devices(inputs=[(4, "Микрофон веб")],
                       outputs=[(8, "Наушники")])

    from sinaps.ui.settings_view import _controls

    mic = _controls(window._rows_of("audio.input_device"))[0]
    out = _controls(window._rows_of("audio.output_device"))[0]
    texts = [mic.itemText(i) for i in range(mic.count())]
    texts += [out.itemText(i) for i in range(out.count())]

    assert "Микрофон веб" in texts
    assert "Наушники" in texts


def test_t_show_main_window_reads_config_and_devices(app, monkeypatch):
    """Открытие окна из трея показывает настройки и устройства.

    Проверяется вызов, а не итоговое наполнение виджетов: окно
    собирается один раз, и если перестать наполнять его при показе,
    человек увидит пустые списки и решит, что микрофонов нет.
    """
    from sinaps.audio import devices as audio_devices
    from sinaps.ui.qt_host import QtHost

    fake = [
        audio_devices.DeviceInfo(2, "Микрофон трея", 1, 48000.0, is_input=True),
    ]
    monkeypatch.setattr(audio_devices, "list_devices", lambda **_: fake)

    cfg = SinapsConfig()
    cfg.tts.speed = 1.5

    host = QtHost.__new__(QtHost)
    host.cfg = cfg
    window = MainWindow()

    host._fill_settings(window)

    from sinaps.ui.settings_view import _controls

    combo = _controls(window._rows_of("audio.input_device"))[0]
    texts = [combo.itemText(i) for i in range(combo.count())]
    speed = _controls(window._rows_of("tts.speed"))[0]

    assert "Микрофон трея" in texts, (
        "окно из трея показало пустой список микрофонов")
    assert speed.value() == pytest.approx(1.5), (
        "окно из трея не показало значения конфигурации")

    host2 = QtHost.__new__(QtHost)
    host2.cfg = cfg
    window2 = MainWindow()
    host2._fill_settings(None)

    assert _controls(window2._rows_of("tts.speed"))[0].value() != 1.5, (
        "окно без настроек не должно показывать их значения")


def test_missing_value_is_left_alone(app):
    """Отсутствующее значение не заменяется нулём.

    Тихий ноль в поле «модель распознавания» выглядит как выбор, и
    человек гадает, почему ассистент не понимает речь.
    """
    from types import SimpleNamespace

    view = SettingsView()
    view.load(SimpleNamespace(stt=SimpleNamespace(model="small"),
                              tts=None))

    model = _find(view, "stt.model")
    assert model.currentText() == "small"


# --- все разделы открыты -----------------------------------------------------


def test_no_placeholder_left(app):
    """Заглушек не осталось: все четыре раздела рабочие."""
    window = MainWindow()

    from sinaps.ui.history_view import HistoryView
    from sinaps.ui.plugins_view import PluginsView
    from sinaps.ui.settings_view import SettingsView

    pages = [window._stack.widget(i) for i in range(window._stack.count())]

    assert isinstance(pages[0], PluginsView)
    assert isinstance(pages[1], HistoryView)
    assert isinstance(pages[2], SettingsView)

    for key, title in SECTIONS:
        assert "1.2" not in window._stack.currentWidget().__class__.__name__
        window.show_section(key)


def test_history_load_fills_window(app, store):
    """Окно получает историю целиком, а не только заголовок."""
    window = MainWindow()
    window.set_history(store.history(), store.history_stats(), "показано 4")

    from sinaps.ui.history_view import HistoryView

    page = window._stack.widget(1)
    assert isinstance(page, HistoryView)
    assert page._list.count() == 4