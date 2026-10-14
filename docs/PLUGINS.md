# Руководство автора плагинов

Sinaps — локальный голосовой ассистент. Плагины расширяют его возможности,
добавляя новые команды, не меняя код ядра.

## Структура плагина

Каждый плагин — отдельная папка в `plugins/`:

```
plugins/
└── my_plugin/
    ├── manifest.yaml      # обязателен
    ├── plugin.py          # код плагина (имя можно любое)
    └── ...                # любые другие файлы
```

**Важно:** папка `plugins/` есть в репозитории, но **пользовательские плагины
игнорируются `.gitignore`** — твои плагины остаются только у тебя.

---

## manifest.yaml

Обязательные 5 полей:

```yaml
name: my_plugin              # латиница, цифры, подчёркивание, без пробелов
version: 1.0.0               # semver: 1, 1.0, 1.0.0
description: Мой плагин       # одно предложение
entry: plugin:register       # модуль:функция
permissions:
  - state                    # права, см. ниже
```

Необязательные:
```yaml
author: Иван
license: MIT
api_version: 1              # пока всегда 1
timeout_s: 30               # таймаут команды, 1..300 (по умолчанию 30)
```

**Имя плагина (`name`)** становится префиксом для всех команд:
`my_plugin.command_name`. Имя папки и `name` должны совпадать.

**`entry`** — формат `модуль:функция`. Функция должна существовать и
возвращать `Skill`, список `Skill` или `None`.

---

## Права (permissions)

| Право | Что даёт | Опасность |
|---|---|---|
| `network.http` | HTTP-запросы через прокси ядра | сеть — нужна проверка |
| `state` | своё пространство в SQLite | низкая |
| `files.read` | чтение файлов | средняя |
| `files.write` | запись файлов | высокая |
| `clipboard.read` | чтение буфера | средняя |
| `clipboard.write` | запись в буфер | низкая |
| `notify` | уведомление в трее | низкая |
| `platform.apps` | запуск/закрытие программ | средняя |
| `platform.media` | звук и медиаплеер | низкая |
| `platform.power` | питание (всегда с подтверждением) | критическая |

**Принцип:** без одобрения плагин **загружается**, но запрос к защищённой
возможности падает с `PermissionDenied`. Человек видит в настройках:
«Плагин X просит network.http — одобрить?» и решает сам.

Одобрение хранится в `data/plugins.json` с хешем набора прав. Если автор
добавит право в манифест, старое одобрение перестанет действовать — нужно
подтвердить заново.

---

## API плагина

Импортируй **только** отсюда:
```python
from sinaps.plugins.api import (
    Skill, IntentSpec, SlotSpec, Response,
    SkillRegistry, Match,
    DialogContext,
    SinapsError, SkillError, SkillNotFound,
    InvalidSlot, DangerousActionDenied,
    PluginError, NetworkDisabled,
    get_dialogue_logger,
)
```

Всё остальное из `sinaps.*` — внутреннее, может измениться без предупреждения.

---

## Пишем плагин

```python
# plugin.py
from sinaps.plugins.api import Response, Skill, SlotSpec


async def get_weather(ctx, slots):
    city = slots.get("city", "").lower()
    # логика...
    return Response(speech=f"В {city} сейчас 20°C, солнечно")


def register(registry):
    skill = Skill("weather", description="Погода")
    skill.command(
        "current",
        get_weather,
        patterns=[r"^погода\s+в\s+(?P<city>.+)$"],
        slots={"city": SlotSpec(name="city", type="string", required=True)},
        examples=["погода в Москве"],
        description="Текущая погода в городе",
    )
    registry.add(skill)
    return skill
```

### Skill

```python
skill = Skill("name", description="Описание")
```

### skill.command()

```python
skill.command(
    "command_name",                    # имя команды → my_plugin.command_name
    handler,                           # async def handler(ctx, slots) -> Response
    patterns=[r"^regex$"],             # список regex для прямого сопоставления
    slots={                            # слоты, извлекаются из named groups
        "city": SlotSpec(
            name="city",
            type="string",             # string|number|duration|time|datetime|bool|path|freeform
            required=True,
            default=None,
            description="Название города",
            min_length=1,
        ),
    },
    examples=["погода в Москве"],      # для панели «что ты умеешь»
    description="Краткое описание",
    dangerous=False,                   # True → потребует голосового подтверждения
    priority=0,                        # выше — проверяется раньше
    hidden=False,                      # не показывать в панели
)
```

**Именованные группы** в regex (`(?P<city>.+)`) становятся ключами словаря
`slots`. Слоты валидируются автоматически: тип, обязательность, длина.

### Response

```python
Response(
    speech="Что сказать вслух",        # обязательно
    display="Что показать в UI",       # опционально
    follow_up="Ещё что-нибудь?",       # продолжение диалога
    ask_for="city",                    # спросить уточнение у пользователя
    show_panel=True,                   # попросить открыть панель
    meta={"ключ": "значение"},         # любые JSON-данные
)
```

### DialogContext (приходит в handler)

```python
ctx.text                # текст фразы пользователя
ctx.source              # "voice" | "text"
ctx.confidence          # уверенность STT (0..1)
ctx.history             # [{"role": "user|assistant", "content": "..."}]
ctx.user_name           # имя пользователя ("Капитан" по умолчанию)
ctx.pending_intent      # намерение, ждущее уточнения
ctx.platform            # ТОЛЬКО для чтения! Не сохраняй, не вызывай напрямую.
ctx.state               # PluginState — своё пространство (если дано право state)
```

---

## Состояние (state)

Если в манифесте есть `permissions: [state]`, в handler приходит
`ctx.state` — асинхронный клиент к своей таблице в SQLite:

```python
async def handler(ctx, slots):
    # прочитать
    n = await ctx.state.get("counter", 0)
    # записать
    await ctx.state.set("counter", n + 1)
    # удалить
    await ctx.state.delete("old_key")
    # все ключи
    keys = await ctx.state.keys()
    # всё целиком
    all_data = await ctx.state.all()
    # стереть всё
    await ctx.state.clear()
```

**Изоляция:** каждый плагин видит только свою таблицу. `PluginState("my_plugin")`
не видит данные `PluginState("other")`. База — `data/plugins.db`.

---

## Ошибки

Бросай только ошибки из API:
```python
from sinaps.plugins.api import NetworkDisabled, PermissionDenied

raise PermissionDenied("state", "my_plugin")
raise NetworkDisabled("нужно разрешение network.http")
```

В лог пойдёт подробность, человеку — `exc.speech` («Эта функция недоступна…»).
Голый `Exception` превратится в «Что-то сломалось, попробуй ещё раз» — так
не делай.

---

## Тестирование

Используй обвязку — она не требует живого ассистента:

```python
import pytest
from sinaps.plugins.testing import plugin_under_test, run_phrase, FakePlatform


async def test_my_plugin(tmp_path):
    # Если нужен state — добавь grant=("state",)
    async with plugin_under_test(
        tmp_path / "my_plugin",
        grant=("network.http", "state"),
    ) as registry:
        # прогон фразы через настоящий реестр
        response = await run_phrase(
            registry,
            "погода в Москве",
            platform=FakePlatform(),  # заглушка ОС, пишет вызовы в .calls
        )

        assert "Москва" in response.speech
        assert response.display is not None
```

**`plugin_under_test`** изолирует всё: файл одобрений, базу состояний,
дочерний процесс. После `async with` всё закрывается и чистится.

---

## Отладка

| Что | Как |
|---|---|
| Логи плагина | `data/logs/sinaps.log` — поищи имя плагина |
| Проверка манифеста | `run_sinaps.bat selftest` валидирует все плагины |
| Список команд | `run_sinaps.bat commands` — покажет твои команды с префиксом |
| Пробный прогон | `run_sinaps.bat try "погода в Москве"` — прогон без выполнения |
| Живой прогон | `run_sinaps.bat listen` — один цикл прослушивания |
| Тесты с процессами | `pytest tests/test_loader.py -v` — реальные дочерние процессы |

**Частая ошибка:** импорт `urllib.request` на уровне модуля. Песочница
блокирует сеть при загрузке модуля. Решение — импорт **внутри функции**:

```python
async def handler(ctx, slots):
    import urllib.request  # внутри функции — ок
    ...
```

---

## Пример: погода

См. `plugins/weather/` — полноценный пример с `network.http`, `state`,
Open-Meteo API, склонением городов, прогнозом на неделю.

Команды:
- `погода в Москве` — текущая
- `погода завтра в Питере` — на завтра
- `прогноз на неделю в Новосибирске` — на 7 дней

---

## Публикация

1. Создай папку `plugins/твой_плагин/`
2. Положи `manifest.yaml` и код
3. Дай права в настройках ассистента (панель → Настройки → Плагины)
4. Готово

Если хочешь поделиться — просто отправь папку плагина. Получатель кладёт
в свой `plugins/`, даёт права — работает. Никаких пакетов, pip, реестров.

---

## Ограничения и будущее

| Сейчас | В планах |
|---|---|
| Только Python | Любой язык через stdin/stdout |
| Только Windows | Linux/macOS — когда будет `platform/` |
| Нет LLM-планировщика | Намерения через LLM для составных команд |
| Нет таймеров/напоминаний | `state` + фоновые задачи v1 |

---

## Вопросы?

- `ARCHITECTURE.md` — как это устроено внутри
- `docs/КОМАНДЫ.md` — список всех команд (генерируется)
- `docs/HOTKEYS.md` — клавиши и кнопки мыши
- Issues на GitHub — для багов и предложений