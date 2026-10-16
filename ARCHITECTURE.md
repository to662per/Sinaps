# Sinaps — архитектура голосового ассистента

Версия документа: 1.1. Статус: **этапы 0, 1 и 2 реализованы**, см. раздел 10.

**Принятые решения (ответы владельца проекта):**
- Строго локальная работа. Облачные движки исключены, бюджет — 0 ₽.
- Работа последовательно: этап 0 → 1 → 2 → 3 → v1 → v2.
- **Целевая платформа — Windows.** Реализации для macOS/Linux не делаются,
  но абстракция `platform` сохранена, чтобы добавление ОС позже не потребовало
  переделки навыков.

Из этого следуют поправки к разделу 3 исходной редакции:

| Было в плане | Стало | Почему |
|---|---|---|
| Гибрид (локально + облако) | **Только локально** | требование владельца |
| Yandex SpeechKit, OpenAI STT/TTS | убраны | платить нечем и нельзя |
| edge-tts | убран | это облако |
| Локальный LLM (Ollama) | отложен до v1 | не нужен для MVP, но место в архитектуре есть |
| TTS по умолчанию | Silero v5 | 150 мс на фразу, 5 русских голосов |

## 0. Что уже есть в репозитории

| Файл | Что делает | Что из этого переиспользуем |
|---|---|---|
| `voice_assistant/main.py` | цикл: хоткей → запись 5 c → Vosk → regex → действие | идея флоу, но переписываем на asyncio + событийную шину |
| `voice_assistant/speech.py` | обёртки над Vosk и pyttsx3 | заменяем на движки с общим интерфейсом `STTEngine` / `TTSEngine` |
| `voice_assistant/commands.py` | regex-разбор команд | идея «дешёвого детерминированного матчера» сохраняется как слой NLU-0 |
| `voice_assistant/actions.py` | запуск приложений, статус системы, открытие файлов | идеи переезжают в навыки `apps`, `system_info`, `files` |
| `voice_assistant/file_index.py` | индекс файлов + нечёткий поиск (`difflib`) | переиспользуем почти как есть, меняем только формат индекса (SQLite FTS5) |
| `voice_assistant/config_manager.py` | JSON-конфиг с дефолтами | заменяем на YAML + dataclass-схему с валидацией |
| `Синапс.py`, `Обучение/*` | учебные скрипты | не трогаем, они остаются как learning-материал |

Вывод: старый код — хороший прототип «уровня 0». Мы его не выкидываем, а
разбираем на модули и переносим в новый пакет `sinaps/`.

---

## 1. Уточняющие вопросы

Блокирующих вопросов нет — ниже список с моими вариантами по умолчанию.
Если что-то не совпадает с твоим выбором, скажи, и я поправлю дизайн до
написания кода.

**По приватности и облаку**
1. Допустимо ли отправлять аудио в облако (Google/Yandex/OpenAI) или нужно
   «всё локально, облако только по явному согласию»?
   *По умолчанию: локальный STT по умолчанию, облако — явный переключатель в
   конфиге, в трее индикатор «микрофон активен».*
2. Есть ли ограничение по бюджету API (₽/мес)? *По умолчанию: 0 ₽, только локальные
   модели + бесплатные API (open-meteo, exchangerate.host, RSS).*

**По железу и языкам**
3. Основной сценарий — только этот ПК (RTX 4060, 16 ГБ RAM) или нужна работа
   на слабом железе без GPU? *По умолчанию: этот ПК — основной, но профиль
   `cpu-only` должен собираться и работать хотя бы медленно.*
4. Нужен ли английский в том же ассистенте, или русский-only достаточно на MVP?
   *По умолчанию: двуязычная архитектура (lang-переключение в настройках), но
   тесты и wake-word только русские.*

**По поведению**
5. Насколько ассистент должен быть «вклиниваемым»? Нужна ли поддержка
   диалога без wake-word (hold-to-talk, «push-to-talk окно»)?
   *По умолчанию: да, hold-to-talk + push-to-talk всегда доступны как
   fallback, если wake-word не сработал.*
6. Нужно ли голосовое подтверждение/отмена («да», «отмена», «стоп») для
   диалога? *По умолчанию: да, но только для опасных действий и только один
   явный шаг подтверждения.*
7. Какие навыки — абсолютный must-have на MVP? *По умолчанию: таймер,
   будильник/напоминание, заметки, время/дата, погода, открыть приложение,
   громкость/пауза, поиск в интернете, выключение по подтверждению.*

**По упаковке и распространению**
8. Это только для себя или планируется передача другим?
   *По умолчанию: только для себя → MVP без установщика, запуск из venv +
   ярлык в автозагрузку. PyInstaller- сборка — в v1.*
9. Нужна ли веб-панель/настройки в браузере или достаточно десктопного окна?
   *По умолчанию: локальное десктопное окно (Tauri/PySide), веб — v2.*

**По существу**
10. Готов ли ты тратить время на дообучение wake-word модели (это отдельная
    история на несколько вечеров) или на старте хватает хоткея?
    *По умолчанию: MVP без дообучения (wake-word через лёгкий KWS-детектор),
    модель — в v1 как отдельная задача.*

---

## 2. Архитектура

### 2.1. Поток данных

```
 ┌──────────────┐   16 kHz mono int16 фреймы (10–20 ms)
 │ Микрофон     │──────────────┐
 └──────────────┘              ▼
                    ┌───────────────────────┐
                    │ AudioCapture (поток)  │  sounddevice/PortAudio
                    │  + кольцевой буфер    │  pre-roll 500 ms
                    └───────────┬───────────┘
                                ▼
              ┌─────────────────────────────────────┐
              │  Ветка 1: WakeWordEngine (всегда)   │  ~0.3 % CPU
              │  «Синапс / Sinaps» + хоткей         │
              └──────────────┬──────────────────────┘
                    триггер │ WakeEvent (+ pre-roll)
                             ▼
              ┌─────────────────────────────────────┐
              │  Ветка 2: VAD (Silero) + STT        │
              │  utterance → текст (partial/final)  │
              └──────────────┬──────────────────────┘
                             ▼
              ┌─────────────────────────────────────┐
              │  NLU: нормализация → слоты          │
              │   1) RuleMatcher (regex, <1 мс)     │
              │   2) LLM fallback (tool calling)    │
              │   3) контекст/память диалога        │
              └──────────────┬──────────────────────┘
                             ▼
              ┌─────────────────────────────────────┐
              │  IntentRouter → SkillRegistry        │
              │  валидация аргументов + подтверждение │
              └──────────────┬──────────────────────┘
                             ▼
              ┌─────────────────────────────────────┐
              │  Навык → Platform capabilities       │  Windows/macOS/Linux
              │  → Integrations (API) → Memory (DB) │
              └──────────────┬──────────────────────┘
                             ▼
              ┌─────────────────────────────────────┐
              │  Response → TTS → воспроизведение    │
              │  (barge-in: речь юзера глушит TTS)  │
              └─────────────────────────────────────┘
```

### 2.2. Машина состояний сессии

Один диалог = один конечный автомат. Он же защищает от гонок (двойное
нажатие хоткея) и даёт точку для barge-in.

```
IDLE ──wake/hotkey/ptt──▶ LISTENING ──silence 600ms──▶ TRANSCRIBING
  ▲                            ▲                              │
  │                            └───────wake/hotkey (отмена)───┘
  │                                                           ▼
  └──SPEAKING◀──utterance── THINKING ◀──intent+slots── ROUTING
                    │                        │
                    └──error──▶ ERROR ───────┘
```

Правила:
- `barge-in`: во время `SPEAKING` энергия речи выше порога → немедленно
  останавливаем воспроизведение, переходим в `LISTENING`.
- Таймауты: `LISTENING` — 8 с без речи → `IDLE`; `THINKING` — 10 с → fallback
  на «не понял».
- Все переходы идут через один `SessionController`, который сериализует
  доступ (никаких блокировок вручную по коду).

### 2.3. Модули

| Модуль | Ответственность | Ключевые интерфейсы |
|---|---|---|
| `core` | Шина событий, FSM, оркестратор, конфиг, логирование, таймеры | `EventBus`, `SessionController` |
| `audio` | Захват с микрофона, кольцевой буфер + pre-roll, воспроизведение, ресемплинг, детекция «пользователь заговорил» (энергия) | `AudioCapture`, `AudioPlayer`, `LoopbackMonitor` |
| `wake` | Wake-word и хоткеи. Несколько бэкендов: `kws_vosk`, `openwakeword`, `hotkey` | `WakeWordEngine` |
| `vad` | Silero VAD: границы речи, снятие шума, удержание тишины 500–700 мс | `VoiceActivityDetector` |
| `stt` | Текст из аудио. Бэкенды: `faster_whisper`, `vosk`, `openai`, `yandex` | `STTEngine.transcribe()`, `.transcribe_partial()` |
| `tts` | Текст в звук. Бэкенды: `silero`, `piper`, `edge`, `yandex` | `TTSEngine.speak()`, `.speak_stream()` |
| `nlu` | Нормализация, слоты, rule-матчер, LLM-fallback с tool calling, диалоговый контекст | `Intent`, `Slot`, `IntentResolver` |
| `skills` | Реестр навыков. Одна декларация `IntentSpec` = источник истины для regex, LLM-схемы и справки | `Skill`, `SkillRegistry` |
| `platform` | Возможности ОС: приложения, окна, звук, медиа, ввод текста, буфер, скриншот, питание, яркость. Адаптеры на ОС | `PlatformCapabilities` |
| `memory` | SQLite: сессии, реплики, факты о пользователе, заметки, таймеры/напоминания, индекс файлов | `MemoryStore` |
| `integrations` | Внешние API: погода, валюты, новости, поиск, перевод, почта, календарь | `Integration` (HTTP-клиент с кэшем и ретраями) |
| `ui` | Трей, главное окно (плагины, история, настройки, справка), панель «что ты умеешь», текстовая консоль, лог-вьювер, индикатор записи | `UiFacade` (события в/из ядра) |
| `plugins` | Загрузка внешних навыков из папки/пакетов, манифест + изоляция | `PluginLoader` |

Правило зависимостей: `ui` и `skills` знают только про `core`. `core` не знает
ни про что конкретное. Никаких `import` между скиллами — только через
реестр. Это и есть цена «модульности»: правило зависимостей должно быть
проверяемым (см. `tests/test_architecture.py` на запрет импортов).

### 2.4. Ключевое решение: одна декларация навыка

`IntentSpec` — единственное место, где описана команда. Из неё
автоматически генерируются: regex-паттерны для быстрого пути, JSON-схема
инструментов для LLM, запись в справку, примеры для eval-набора.

```python
IntentSpec(
    name="timer.set",
    patterns=[r"поставь таймер на (?P<duration>.+)"],
    examples=["поставь таймер на пять минут", "таймер 90 секунд"],
    slots={"duration": SlotSpec(type="duration")},
    dangerous=False,
    handler=set_timer,          # async def (ctx, slots) -> Response
)
```

Следствия:
- новый навык = один файл + одна строка в реестре; LLM-схема обновляется сама;
- нельзя «забыть» добавить команду в LLM — она строится из того же реестра;
- rule-матчер и LLM не могут разойтись в том, какие команды вообще есть.

### 2.5. Потокобезопасность и конкурентность

Один процесс, один event loop (`asyncio`), аудио — в отдельном потоке
(callback Portaudio), всё остальное — корутины. Причины:
- `sounddevice`/Whisper/ONNX — блокирующие или CPU-bound → уводим в
  `run_in_executor` с отдельным пулом для тяжёлых задач;
- «Блокирующий» вызов TTS не должен стопорить barge-in → воспроизведение
  всегда в отдельной задаче, с cooperative-отменой;
- таймеры/напоминания — просто asyncio-таски, живущие в памяти, плюс запись в
  SQLite для восстановления после перезапуска.

Никаких глобальных переменных состояния (в текущем прототипе `recognizer` и
`file_idx` — глобалки; в новой версии их не будет, всё через контейнер).

### 2.6. Приватность и безопасность

- Микрофон: по умолчанию **не** пишется на диск. Кольцевой буфер в памяти,
  pre-roll 500 мс.
- Индикатор в трее: «слушаю / думаю / говорю / микрофон выключен».
- Настройка `privacy.record_audio` (по умолчанию `false`) — для отладки и
  разбора ошибок распознавания.
- Разделение данных: `history` (что говорили) и `facts` (что запомнили о
  пользователе) — разные таблицы, разные сроки хранения, удаление одной
  кнопкой.
- Опасные действия (`power.shutdown`, `files.delete`, `shell.run`) требуют
  явного подтверждения голосом или хоткеем. Подтверждение имеет TTL 10 с и
  одноразовость — нельзя сказать «да» через минуту на другую команду.
- Никакого `shell=True` из пользовательского ввода; команды запускаются только
  по белому списку путей/алиасов.
- Сетевые интеграции идут через один слой с логом «что ушло наружу».

### 2.7. Модель «намерение → действие» и диалог

- `DialogContext`: последние N ходов, текущие слоты, ожидающий ввод
  (например, после «напомни мне о…» → ждём время).
- Уточняющий вопрос вместо догадки: если слот обязателен и не распознан —
  навык возвращает `Response.ask(...)`, FSM переходит в `LISTENING` с
  установленным `pending_intent`.
- Fallback-цепочка: `rule → LLM → "не понял, вот что я умею"`. Отказоустойчиво:
  если LLM недоступен, ассистент продолжает работать на regex-командах.

---

## 3. Стек: три варианта

### Вариант A. Полностью локальный

| Слой | Технология |
|---|---|
| Wake | `sherpa-onnx` KWS или `openWakeWord` (свой русский) + `pynput`/`keyboard` |
| VAD | `silero-vad` (ONNX, <1 мс на CPU) |
| STT | `faster-whisper` (CUDA), модель `small`/`medium`; `vosk-model-small-ru-0.22` как быстрый fallback |
| TTS | `silero-tts` v5 (ru), опционально `piper` для качества |
| NLU | rule-матчер + `Ollama` c локальной моделью (qwen3:4b / qwen2.5:3b) |
| Планировщик/БД | SQLite, APScheduler-like asyncio-таски |

- Плюсы: приватность 100 %, нет затрат, офлайн, предсказуемая задержка.
- Минусы: качество распознавания заметно ниже облака; своя модель wake-word
  для русского = отдельный проект; TTS звучит «роботнее».
- Стоимость: 0 ₽. Задержка: 1.0–1.8 с (medium на 4060 ~0.35 с на фразу 4 с).
- Сложность: высокая в части ML-обвязки, низкая в части API.

### Вариант B. Облачный

| Слой | Технология |
|---|---|
| Wake | локально (`sherpa-onnx` KWS / `openWakeWord`) — в облако нельзя, иначе постоянный трафик |
| VAD | локально (`silero-vad`) |
| STT | `Yandex SpeechKit` (лучшее качество RU) / `OpenAI gpt-4o-transcribe` / `Google` |
| TTS | `edge-tts` (бесплатно, отличное качество, нет ключа) или `Yandex SpeechKit` |
| NLU | `GPT-4o-mini` / `Claude Haiku` / `Gemini Flash` с function calling |
| Планировщик/БД | SQLite локально |

- Плюсы: лучшее качество RU-распознавания и TTS, сильный NLU из коробки.
- Минусы: зависимость от сети и от чужих SLA; аудио уходит наружу; ~300–600 мс
  только на сеть туда-обратно.
- Стоимость: SpeechKit ≈ 0,6 ₽/мин аудио (в режиме потока) — при 10 командах
  в день это копейки; edge-tts — бесплатно. LLM: цена зависит от модели,
  можно уложиться в 0 ₽ на flash-моделях.
- Сложность: низкая.

### Вариант C. Гибридный — **рекомендую**

Базовый путь локальный, облако только там, где локальный слой не тянет:

```
wake + VAD                       → всегда локально
STT   → faster-whisper small     → по умолчанию локально
       (fallback: облако)          если confidence < 0.6 или движок не загружен
TTS   → silero v5                 → по умолчанию локально
       (профиль «естественный»: edge-tts)
NLU   → rule-матчер               → всегда локально, <1 мс
       (LLM)                      → Ollama qwen3:4b локально; облако — флаг
```

- Плюсы: работает без сети; качество как у облака там, где это важно;
  приватность под контролем (по умолчанию наружу не уходит ничего).
- Минусы: две реализации каждого слоя → больше кода и тестов; нужно чётко
  следить за метриками «пошли в облако».
- Задержка (p50 на этом ПК): wake → первый звук ответа ≈ 1.2–1.6 с.
- Сложность: средняя.

### 3.1. Почему именно так на твоём железе

RTX 4060 8 ГБ, 16 ГБ RAM, 12 логических ядер:

- `faster-whisper medium` (float16) занимает ~2.5 ГБ VRAM — влезает с
  запасом; `large-v3-turbo` (~1.6 ГБ int8/fp16) тоже влезает, но на CPU
  часть слоёв уйдёт при нехватке VRAM — берём medium как баланс.
- `silero-vad` и `silero-tts` — CPU, <50 МБ, задержка десятки мс.
- `qwen3:4b` Q4_K_M (~2.5 ГБ) + Whisper medium одновременно в 8 ГБ VRAM
  помещаются — можно держать локальный LLM постоянно прогруженным, тогда
  TTFT ~0.3–0.5 с.

### 3.2. ⚠️ Важно: версия Python

В системе стоит **Python 3.14.2**, и под него **нет wheels** у
`ctranslate2` (движок `faster-whisper`) и `torch` в части CUDA-сборок.
`faster-whisper` официально рассчитан на 3.9–3.12.

**Решение: весь проект — на Python 3.12**, изолированно.
Рекомендую `uv` (быстрый, создаёт venv нужной версии сам):

```powershell
winget install --id=astral-sh.uv
uv python install 3.12
cd D:\Sinaps
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
```

У тебя уже есть Astral 3.12.12 (`py -V:Astral\CPython3.12.12`), так что
можно и без установки uv. Но `uv` удобнее — с ним же потом будет
`requirements.lock` для воспроизводимости.

### 3.3. Wake-word: честно о сложности

- Готовые модели `openWakeWord` — **только английские**.
- Русское wake-word ⇒ либо дообучение (нужныpositive-примеры голоса +
  генерация негативов; отдельная задача на несколько вечеров, хрупкая по
  качеству), либо KWS-модель `sherpa-onnx` (есть ru-трансформеры, но
  keyword-таблица под русский тоже собирается руками), либо дешёвый
  трюк: лёгкий Vosk-детектор с ограниченной грамматикой
  (`["синапс", "sinaps", "[unk]"]`) — работает, но жрёт больше CPU и хуже
  по FAR.
- **План:** MVP = хоткей + push-to-talk + «кто-то сказал» (энергия/VAD);
  v1 = отдельная задача на wake-word; v2 = openWakeWord с дообучением.

Это самый рискованный пункт всего проекта, поэтому он не в MVP.

---

## 4. Структура проекта

```
D:\Sinaps\
├── pyproject.toml              # метаданные, зависимости, ruff/pytest
├── uv.lock / requirements.lock # воспроизводимая среда
├── ARCHITECTURE.md             # этот документ
├── README.md                   # быстрый старт
├── run_sinaps.bat              # запуск venv (и pythonw-вариант для трея)
│
├── sinaps\                     # ← основной пакет
│   ├── __main__.py             # точка входа: разбор --flag'ов, запуск ядра
│   ├── core\
│   │   ├── bus.py              # EventBus: подписка, публикация, приоритеты
│   │   ├── session.py          # FSM + сериализация диалогов
│   │   ├── orchestrator.py     # сборка пайплайна, DI-контейнер
│   │   ├── config.py           # YAML + dataclass + валидация + hot reload
│   │   ├── logging_setup.py    # ротация, уровни по модулям, лог диалогов
│   │   └── errors.py           # типизированные исключения
│   ├── audio\
│   │   ├── capture.py          # поток Portaudio + кольцевой буфер + pre-roll
│   │   ├── player.py           # воспроизведение, очередь, прерывание
│   │   ├── mixer.py            # ducking: приглушить себя во время речи
│   │   └── devices.py          # выбор устройства, hot-swap
│   ├── wake\
│   │   ├── base.py             # WakeWordEngine (ABC)
│   │   ├── kws_vosk.py         # KWS через Vosk с грамматикой
│   │   ├── openwakeword.py     # v1+
│   │   └── hotkey.py           # pynput/keyboard, режим удержания
│   ├── vad\silero_vad.py
│   ├── stt\
│   │   ├── base.py             # STTEngine (ABC): partial + final
│   │   ├── faster_whisper.py
│   │   ├── vosk_stream.py
│   │   └── cloud.py            # OpenAI / Yandex
│   ├── tts\
│   │   ├── base.py             # TTSEngine (ABC) + число прописью
│   │   ├── silero_tts.py       # Silero v5 (основной)
│   │   ├── piper_tts.py        # Piper (качественнее, голоса отдельно)
│   │   ├── pyttsx3_tts.py      # системный SAPI5 (запасной)
│   │   └── null_tts.py         # заглушка для тестов
│   ├── nlu\
│   │   ├── normalize.py        # числа, даты, опечатки, «синапс, ...» в начале
│   │   ├── slots.py            # SlotSpec + парсеры (duration, time, number)
│   │   ├── rules.py            # NLU-0: regex-матчер по реестру
│   │   ├── llm.py              # tool-calling, таймауты, JSON-схема
│   │   ├── context.py          # DialogContext, pending_intent, история
│   │   └── resolver.py         # цепочка rule → LLM → fallback
│   ├── skills\
│   │   ├── base.py             # Skill, IntentSpec, Response, SkillRegistry
│   │   ├── registry.py
│   │   ├── apps.py             # открыть/закрыть/найти приложение
│   │   ├── windows.py          # фокус, свернуть, закрыть, переключить
│   │   ├── media.py            # play/pause/next, громкость, mute
│   │   ├── system_info.py      # psutil: cpu/ram/disk/battery/сеть
│   │   ├── files.py            # поиск по индексу, открыть, показать папку
│   │   ├── typing.py           # ввод текста, буфер обмена, скриншот
│   │   ├── power.py            # сон/выключение/перезагрузка (confirm!)
│   │   ├── time_date.py        # время, дата, «сколько времени»
│   │   ├── timer.py            # таймеры, напоминания (asyncio + SQLite)
│   │   ├── notes.py            # заметки
│   │   ├── web.py              # поиск, открыть сайт, YouTube
│   │   ├── weather.py          # open-meteo
│   │   ├── translate.py
│   │   └── smalltalk.py        # приветствие, кто ты, спасибо
│   ├── platform\
│   │   ├── base.py             # Capabilities-протоколы
│   │   ├── windows.py          # pywin32, ctypes, uiautomation/keyboard
│   │   ├── macos.py            # osascript, pynput
│   │   ├── linux.py            # xdotool/pactl/playerctl
│   │   └── detect.py
│   ├── memory\
│   │   ├── store.py            # SQLite: сессии, реплики, факты
│   │   ├── schema.sql
│   │   ├── timers.py           # персистентные таймеры
│   │   └── file_index.py       # перенос старого индекса на SQLite FTS5
│   ├── integrations\
│   │   ├── http.py             # httpx + кэш + ретраи + «что ушло наружу»
│   │   ├── weather.py  currency.py  news.py  search.py  translate.py
│   ├── ui\
│   │   ├── tray.py             # pystray + Pillow, статус, меню
│   │   ├── window.py           # окно: лог, история, настройки
│   │   ├── console.py          # текстовый ввод/вывод (работает без GUI)
│   │   └── overlay.py          # «Слушаю…» поверх окон (позже)
│   └── plugins\loader.py       # внешние навыки
│
├── plugins\                    # пользовательские навыки (каталог)
├── tests\
│   ├── fixtures\               # wav-фразы 20–30 шт + эталонные тексты
│   ├── unit\                   # nlu, slots, skills, memory
│   ├── integration\            # pipeline с фейковыми STT/TTS
│   ├── eval\                   # eval-набор команд + метрики
│   └── test_architecture.py    # проверка правил зависимостей
├── models\                     # локальные модели (в git не коммитим)
├── data\                       # sinaps.db, логи, кэш (в git не коммитим)
└── legacy\voice_assistant\     # старый прототип, если решим сохранить
```

---

## 5. План работ

### Этап 0. Фундамент (2–3 дня)

1. `uv` + venv 3.12 + `pyproject.toml` + базовые зависимости.
2. Каркас `core`: `EventBus`, `config`, `logging`, `SessionController` (FSM).
3. `sinaps --selftest`: запускается, печатает конфиг и состояние, выходит.

**Готово когда:** `python -m sinaps --selftest` на любой машине проходит без
микрофона и без сети.

### Этап 1. Голос в текст и обратно (3–5 дней)

4. `audio/capture.py`: поток + кольцевой буфер + pre-roll 500 мс.
5. `vad/silero_vad.py`: границы фразы, тишина 600 мс, отсечение шума.
6. `stt/faster_whisper.py` + `stt/vosk_stream.py`, переключатель в конфиге.
7. `tts/silero_tts.py` + `tts/edge_tts.py`.
8. `ui/console.py`: ввод текстом + озвучка ответа. Уже работает «эхо»-контур.

**Готово когда:** напечатал в консоли «привет» — услышал ответ голосом; и
сказал в микрофон — появился текст.

### Этап 2. Управление ПК (5–7 дней)

9. `platform/base.py` + `platform/windows.py` (приоритет — Windows).
10. Навыки: `apps`, `windows`, `media`, `system_info`, `files`, `power`,
    `time_date`.
11. `nlu/rules.py` + `normalize.py` + слоты (`duration`, `number`, `query`).
12. Подтверждение опасных действий (`confirm.py` + `pending_intent`).
13. Хоткеи: `wake/hotkey.py` (push-to-talk и toggle).

**Готово когда:** 20 голосовых команд из чек-листа выполняются с точностью
≥ 90 %, выключение ПК требует слова «да».

### Этап 3. MVP-приёмка (2–3 дня)

14. `ui/tray.py` — трей, статус, меню «пауза/выход/микрофон off».
15. `memory/store.py` — история диалогов в SQLite.
16. Метрики задержки (`core/metrics.py`) → вывод в лог и в трей.
17. Тесты (см. раздел 7), README, `run_sinaps.bat`, автозагрузка.

**Итог MVP:** локальный, офлайн, хоткей или PTT, ~30 команд, история, трей.

### Этап 4. v1 (3–4 недели)

- Навыки: `timer`, `notes`, `weather`, `web`, `translate`, `smalltalk`.
- `nlu/llm.py` + Ollama (qwen3:4b) → fallback на естественный язык.
- Диалоговый контекст и уточняющие вопросы.
- Окно настроек (выбор STT/TTS/LLM, голос, хоткей, язык, приватность).
- `integrations`: open-meteo, exchangerate.host, RSS-новости.
- Кроссплатформенные адаптеры `macos.py` / `linux.py`.
- Дообучение wake-word (отдельная задача, см. 3.3).

### Этап 5. v2 (отдельный проект)

- Плагины из внешних папок/пакетов, манифест, песочница прав.
- Умный дом (Home Assistant / MQTT).
- Локальная семантическая память (эмбеддинги в SQLite-vec).
- Голосовая персонализация: свой голос (XTTS), «Капитан» из `Синапс.py`
  как профиль пользователя.
- PyInstaller/установщик, автообновление.

---

## 6. Примеры кода ключевых модулей

> Это скелеты интерфейсов и контрактов, не финальная реализация.
> Каждый блок — минимальный, но показывает форму.

### 6.1. Шина событий (`core/bus.py`)

```python
from __future__ import annotations

import asyncio
import enum
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


class EventType(str, enum.Enum):
    WAKE = "wake"
    SPEECH_START = "speech_start"
    TRANSCRIPT_PARTIAL = "transcript_partial"
    TRANSCRIPT_FINAL = "transcript_final"
    INTENT_RESOLVED = "intent_resolved"
    RESPONSE_READY = "response_ready"
    ERROR = "error"


@dataclass(frozen=True)
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Асинхронная шина. Подписчики одного и того же события
    выполняются параллельно; исключения изолируются по подписчику."""

    def __init__(self) -> None:
        self._subs: dict[EventType, list[Handler]] = defaultdict(list)

    def on(self, event_type: EventType) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self._subs[event_type].append(fn)
            return fn
        return deco

    async def publish(self, event: Event) -> None:
        handlers = self._subs.get(event.type, [])
        if not handlers:
            return
        results = await asyncio.gather(
            *(self._safe_call(h, event) for h in handlers),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                log.exception("Ошибка подписчика на %s: %s", event.type, r)

    @staticmethod
    async def _safe_call(handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            log.exception("Подписчик %s упал на %s", handler, event.type)
```

### 6.2. Контракты STT/TTS

```python
# stt/base.py
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import AsyncIterator

import numpy as np
from numpy.typing import NDArray

Float32 = NDArray[np.float32]  # моно 16 kHz


@dataclass
class Transcript:
    text: str
    confidence: float = 1.0
    is_final: bool = True
    language: str = "ru"


class STTEngine(abc.ABC):
    """Любой движок распознавания. Реализации: faster_whisper, vosk, cloud."""

    name: str

    @abc.abstractmethod
    async def transcribe(self, audio: Float32) -> Transcript:
        """Финальное распознавание целой фразы."""

    async def stream(self, chunks: AsyncIterator[Float32]) -> AsyncIterator[Transcript]:
        """Потоковые частичные результаты (по умолчанию — только финальный)."""
        buf: list[Float32] = []
        async for chunk in chunks:
            buf.append(chunk)
        yield await self.transcribe(np.concatenate(buf))
```

```python
# tts/base.py
class TTSEngine(abc.ABC):
    name: str

    @abc.abstractmethod
    async def synthesize(self, text: str) -> Float32:
        """Синтез в PCM 16 kHz mono float32."""

    async def speak(self, text: str, player: "AudioPlayer") -> None:
        """Синтез + воспроизведение с возможностью прерывания."""
        pcm = await self.synthesize(text)
        await player.play(pcm)          # player.play отменяется barge-in'ом
```

### 6.3. Декларация навыка и реестр

```python
# skills/base.py
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ..core.bus import EventBus


@dataclass
class SlotSpec:
    name: str
    type: Literal["string", "number", "duration", "time", "bool", "path"]
    required: bool = True
    default: Any = None


@dataclass
class Response:
    """Результат навыка: что сказать + что показать в UI + действие."""
    speech: str
    display: str | None = None
    follow_up: str | None = None          # например, «ещё что-нибудь?»


class Skill:
    """Навык = набор IntentSpec'ов. Реестр сам строит regex и LLM-схему."""

    name: str
    intents: list["IntentSpec"] = field(default_factory=list)

    def register(self, intent: IntentSpec) -> "IntentSpec":
        self.intents.append(intent)
        return intent


Handler = Callable[["DialogContext", dict[str, Any]], Awaitable[Response]]


@dataclass
class IntentSpec:
    name: str                                     # "timer.set"
    handler: Handler
    patterns: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    slots: dict[str, SlotSpec] = field(default_factory=dict)
    dangerous: bool = False                       # требует подтверждения
    description: str = ""

    _compiled: list[re.Pattern] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._compiled = [re.compile(p, re.IGNORECASE) for p in self.patterns]

    # → сюда же попадает generation of JSON schema для LLM
    def to_tool_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "examples": self.examples,
            "parameters": {
                "type": "object",
                "properties": {
                    s.name: {"type": "string", "description": s.type}
                    for s in self.slots.values()
                },
                "required": [
                    s.name for s in self.slots.values() if s.required
                ],
            },
        }
```

```python
# skills/timer.py
from datetime import timedelta

from ..core.bus import EventBus
from .base import IntentSpec, Response, Skill, SlotSpec


def register(bus: EventBus) -> Skill:
    skill = Skill(name="timer")

    async def set_timer(ctx, slots):
        seconds = slots["duration"]                      # уже распарсено слотом
        label = slots.get("label") or "таймер"
        ctx.memory.add_timer(seconds, label)             # персистентно
        return Response(speech=f"Таймер на {ctx.humanize(seconds)}")

    skill.register(IntentSpec(
        name="timer.set",
        handler=set_timer,
        patterns=[r"поставь таймер на (?P<duration>.+)",
                  r"таймер (?:на )?(?P<duration>.+)",
                  r"напомни через (?P<duration>.+)"],
        examples=["поставь таймер на десять минут", "таймер на час"],
        slots={"duration": SlotSpec("duration", "duration"),
               "label": SlotSpec("label", "string", required=False)},
        description="Поставить таймер на заданный промежуток времени",
    ))

    return skill
```

### 6.4. Матчер правил (NLU-0)

```python
# nlu/rules.py
from dataclasses import dataclass

from ..skills.base import IntentSpec


@dataclass
class RuleMatch:
    spec: IntentSpec
    slots: dict[str, str]


class RuleMatcher:
    """Дешёвый первый рубеж: точные команды работают без сети и LLM."""

    def __init__(self, specs: list[IntentSpec]) -> None:
        self._specs = list(specs)

    def match(self, text: str) -> RuleMatch | None:
        for spec in self._specs:
            for rx in spec._compiled:
                m = rx.search(text)
                if m:
                    return RuleMatch(spec=spec, slots=m.groupdict())
        return None
```

### 6.5. LLM-fallback с tool calling

```python
# nlu/llm.py
import json

SYSTEM = """Ты — Sinaps, голосовой ассистент. На основе реплики пользователя
выбери ровно один инструмент из списка и заполни его аргументы.
Если ни один инструмент не подходит — верни {"tool": "none", "reason": "..."}.
Отвечай только JSON, без пояснений."""


class LlmResolver:
    def __init__(self, client, tools: list[dict]) -> None:
        self._client = client          # OpenAI-совместимый / Ollama
        self._tools = tools

    async def resolve(self, text: str, history: list[dict]) -> RuleMatch | None:
        resp = await self._client.chat(
            model="qwen3:4b",
            messages=[{"role": "system", "content": SYSTEM},
                      *history[-6:],
                      {"role": "user", "content": text}],
            tools=self._tools,
            temperature=0.0,
            timeout=8.0,
        )
        call = resp.choices[0].message.tool_calls[0] if resp.choices[0].message.tool_calls else None
        if call is None:
            return None
        spec = self._by_name(call.function.name)
        args = json.loads(call.function.arguments or "{}")
        return RuleMatch(spec=spec, slots=args)
```

### 6.6. Конвейер диалога

```python
# core/orchestrator.py (фрагмент)
async def _on_final_transcript(self, event: Event) -> None:
    text = normalize(event.payload["text"])
    if not text:
        return

    match = self._rules.match(text) or await self._llm.resolve(text, self._history)

    if match is None:
        await self._tts.say("Не понял. Попробуй сказать иначе.")
        return

    slots = self._parse_slots(match)
    if missing := [s for s in match.spec.slots.values() if s.required and s.name not in slots]:
        await self._session.ask_followup(match.spec, missing[0].name)
        return

    if match.spec.dangerous and not await self._confirm.ask(match.spec, slots):
        await self._tts.say("Отменено.")
        return

    try:
        response = await match.spec.handler(self._ctx, slots)
    except SkillError as exc:                      # типизированные ошибки навыка
        await self._tts.say(str(exc))
        return

    self._history.append({"role": "user", "content": text})
    self._history.append({"role": "assistant", "content": response.speech})
    self._memory.save_turn(text, response.speech)
    await self._tts.say(response.speech)
```

### 6.7. Захват аудио с pre-roll

```python
# audio/capture.py (фрагмент)
class AudioCapture:
    def __init__(self, sr: int = 16000, frame_ms: int = 20, pre_roll_ms: int = 500):
        self._sr, self._frame_ms = sr, frame_ms
        self._n_pre = int(sr * pre_roll_ms / 1000)
        self._ring: deque[np.ndarray] = deque(maxlen=self._n_pre)
        self._q: asyncio.Queue[np.ndarray] = asyncio.Queue()

    def _callback(self, indata, frames, time_info, status):  # поток Portaudio
        chunk = indata[:, 0].copy()          # mono
        self._ring.append(chunk)
        self._loop.call_soon_threadsafe(self._q.put_nowait, chunk)

    async def stream(self) -> AsyncIterator[np.ndarray]:
        while True:
            yield await self._q.get()

    def preroll(self) -> np.ndarray:
        """Аудио до момента wake-слова — иначе теряем первое слово команды."""
        return np.concatenate(list(self._ring)) if self._ring else np.empty(0, np.float32)
```

### 6.8. Подтверждение опасного действия

```python
# core/confirm.py
class ConfirmSession:
    TTL = 10.0

    def __init__(self, tts, bus: EventBus) -> None:
        self._tts, self._bus = tts, bus
        self._pending: tuple[str, dict, float] | None = None

    async def ask(self, spec, slots) -> bool:
        self._pending = (spec.name, slots, time.monotonic() + self.TTL)
        await self._tts.say(f"{spec.description}. Подтверди: да или нет.")
        # ждём ответ, пока FSM в состоянии LISTENING_CONFIRM
        confirmed = await self._wait_for_answer()          # из событий STT
        return confirmed == "да" and self._not_expired()

    def consume(self, answer: str) -> bool:
        """Одноразовое подтверждение: сбрасывается при любом ответе."""
        ok = answer.strip().lower().startswith(("да", "ага", "подтверждаю"))
        self._pending = None
        return ok
```

---

## 7. Тесты и критерии приёмки

### 7.1. Пирамида тестов

- **Unit (быстро, без железа).** Нормализация текста, слоты
  (`«на пять минут» → 300 с`, `«в 7:30» → сегодня 19:30`), правила,
  реестр, схемы инструментов, `MemoryStore` на временной БД, файловый индекс
  на фикстурах.
- **Integration.** Полный пайплайн на записанных wav-фразах с подменой
  микрофона на файл и TTS на заглушку. Проверяем: намерение, слоты, факт
  вызова действия, текст ответа.
- **Архитектурные.** `test_architecture.py` запрещает `ui`/`skills` импортировать
  друг друга и запрещает скиллам знать про конкретные ОС напрямую
  (только `platform`). Это защита от «разъезжающейся» модульности.
- **Eval-набор.** 100–150 фраз (`fixtures/commands.jsonl`: аудио → ожидаемое
  намерение + слоты). Метрика — intent accuracy по Top-1.

### 7.2. Критерии приёмки MVP

| Метрика | Порог |
|---|---|
| Хоткей/hold-to-talk срабатывает | 100 %, задержка < 100 мс |
| Wake-word (когда появится) | FAR < 1 ложное срабатывание / 8 ч; FR < 15 % |
| Intent accuracy на eval-100 | ≥ 90 % (rule-путь), ≥ 85 % с LLM-fallback |
| Слоты (время/длительность/числа) | ≥ 95 % на fixtures/commands.jsonl |
| Задержка «после фразы → первый звук ответа», p50 | ≤ 1.6 с |
| То же, p95 | ≤ 3.0 с |
| Опасные действия без подтверждения | 0 выполнений в тесте |
| Отклик трея | < 200 мс |
| Стабильность | 8 ч фоновой работы без падения и без роста RAM > 15 % |
| CPU в покое (только wake+VAD) | ≤ 5 % от одного ядра |

### 7.3. Инструменты измерения

- `core/metrics.py`: тайминги по стадиям (vad_end → stt_done → intent →
  skill → first_audio), скользящее p50/p95, вывод в лог и в трей.
- Бенчмарк: `python -m sinaps --benchmark` — прогоняет 100 фраз из фикстур,
  печатает таблицу задержек и intent accuracy.
- Нагрузочный прогон: `python -m sinaps --soak 8h` с отчётом по памяти.

---

## 8. Риски, ограничения и честные ожидания

### 8.1. Риски

| Риск | Вероятность | Влияние | Митигация |
|---|---|---|---|
| Русский wake-word не обучается до нужного качества | высокая | среднее | не тащить в MVP; хоткей + PTT как основной способ; бюджет времени на обучение закладывать отдельно |
| Vosk/Whisper путают похожие слова («синапс» / «синапсок») | высокая | среднее | ограниченная грамматика для KWS, список похожих слов в конфиге, fallback на хоткей |
| Microphone занят эксклюзивным режимом (Windows WASAPI) | средняя | среднее | захват в shared-режиме, выбор устройства, понятная ошибка в UI |
| Борьба с GIL: TTS/Whisper блокируют loop | высокая | среднее | весь CPU-bound — в executor; измерение p95 в тестах |
| PyInstaller не пакует ONNX/torch-модели | высокая | низкое | сборка только в v1/v2, модели грузить по внешнему пути |
| Локальный LLM галлюцинирует аргументы | средняя | среднее | валидация слотов + whitelist допустимых значений; опасное — только с подтверждением |
| Рост числа навыков → «команда не найдена» | средняя | среднее | автогенерация справки из реестра; «что ты умеешь» всегда отвечает |
| Публичные API деградируют/меняются (RSS, курсы) | средняя | низкое | кэш + последний успешный ответ + честное «данные устарели» |

### 8.2. Что нельзя сделать «как в Siri» быстро

Честно: Siri/Google Assistant/Alexa — это продукт с:
- миллионами часов размеченных данных для NLU, собственной акустикой,
- закрытыми API и собственными моделями,
- командой из десятков инженеров, годами поддержки,
- глубокой интеграцией в ОС (корпоративные политики, MDM, подписанные
  приложения, доступ к системным сервисам вроде маршрутизации вызовов).

Поэтому в MVP **не будет** и не может быть «из коробки»:
- безошибочного распознавания любой фразы на любом акценте и в шуме;
- голоса уровня Siri (близко даёт только облачный TTS, и это отправка данных
  наружу);
- работы в фоне без разрешения пользователя (в Windows доступ к микрофону,
  автозапуск и горячие клавиши — с ограничениями политики);
- полноценной работы с почтой/календарём/мессенджерами: нужны OAuth-приложения,
  каждый API — отдельная интеграция с лимитами и модерацией;
- «энергосберегающего» CPU в фоне — пока нет отдельного low-power DSP-режима.

Реалистичная цель MVP: **локальный ассистент, который надёжно выполняет
30–50 заранее известных команд за < 2 секунды, не отправляя ничего в сеть**.
Это уже полезно. Всё остальное — расширение, а не «недоделанный Siri».

### 8.3. Порядок, в котором стоит расширять

Приоритет по соотношению «польза / стоимость»:

1. Таймеры, напоминания, заметки, время/дата — дёшево, пользователь сразу
   чувствует пользу.
2. Открытие/закрытие приложений, громкость, окна, скриншот — высокая польза,
   средняя сложность.
3. Погода, валюты, поиск — почти бесплатно (готовые API), высокая польза.
4. Диалоговый контекст и уточняющие вопросы — превращает набор команд в
   ассистента.
5. Wake-word, локальный LLM, плагины, умный дом — дорого, откладываем.

---


## 10. Что реализовано

Разделы ниже описывают состояние на конец Alpha 3 — аудио, команды, платформу
и интерфейс. С тех пор добавилась **система плагинов целиком** (раздел 13)
и появился первый плагин в поставке. Здесь же перечислены найденные при
этом баги: список сохранён целиком, он и есть главная ценность этого раздела.

### Состав

| Слой | Файл | Состояние |
|---|---|---|
| Ядро: шина событий | `sinaps/core/bus.py` | готово, 24 типа событий |
| Ядро: FSM | `sinaps/core/session.py` | готово, таблица переходов |
| Ядро: конфиг | `sinaps/core/config.py` | готово, YAML + валидация + hot reload |
| Ядро: логи | `sinaps/core/logging_setup.py` | готово, ротация + лог диалогов |
| Ядро: метрики | `sinaps/core/metrics.py` | готово, p50/p95 по стадиям |
| Ядро: контекст | `sinaps/core/context.py` | готово |
| Ядро: ответ | `sinaps/core/response.py` | готово, общий контракт слоёв |
| Аудио: захват | `sinaps/audio/capture.py` | кольцевой буфер + pre-roll 500 мс |
| Аудио: источник | `sinaps/audio/source.py` | микрофон ИЛИ wav-файл |
| Аудио: воспроизведение | `sinaps/audio/player.py` | прерываемое |
| VAD | `sinaps/vad/silero_vad.py` | Silero + запасной energy |
| STT | `sinaps/stt/*` | faster-whisper + vosk + заглушка |
| TTS | `sinaps/tts/*` | Silero v5 + Piper + pyttsx3 + заглушка |
| NLU | `sinaps/nlu/*` | нормализация, слоты, числа прописью |
| Навыки | `sinaps/skills/*` | 35 команд в 9 навыках |
| Возможности ОС | `sinaps/platform/*` | Windows |
| Контур речи | `sinaps/conversation/*` | готово |
| Консоль | `sinaps/ui/console.py` | готово |
| Сборка | `sinaps/runtime.py` | готово |

Тесты: **516**, из них 60 архитектурных. Прогон — 9 секунд, без микрофона
и без сети.

### Реализованные команды (36)

| Навык | Команды |
|---|---|
| Время и дата | `time.now`, `time.date`, `time.weekday` |
| Общение | `smalltalk.greeting/farewell/thanks/identity/capabilities` |
| Программы и файлы | `apps.open/close/list/open_file/open_folder/reveal` |
| Звук | `media.volume_up/volume_down/mute/level/play_pause/next/previous` |
| Система | `system_info.status/param` |
| Экран | `screen.screenshot/brightness` |
| Буфер обмена | `clipboard.copy/paste` |
| Окна | `windows.list/focus/close/minimize_all` |
| Питание | `power.shutdown/restart/sleep/lock` — **все с подтверждением** |

### Измеренные факты (не предположения)

- **Задержка ответа 0.31 с (p50) / 0.47 с (p95)** на RTX 4060 — требование
  «< 1–2 с» выполняется с трёхкратным запасом.
- Распознавание 39 типовых фраз: **39 из 39**.
- Сканирование установленных программ: 100.8 с → **1.1 с** после перехода на
  один вызов PowerShell вместо одного на каждый ярлык.
- Silero TTS v5: ~150 мс на фразу в 2–3 с речи, вывод **детерминированный**.
- Silero VAD требует чанки ровно по 512 сэмплов — внутри есть буферизация.
- Silero TTS v5 принимает только 8000/24000/48000 Гц.
- Silero TTS v5 **не имеет параметра `speed`** — темп через пересемплирование.
- faster-whisper на Windows требует `os.add_dll_directory` для
  `site-packages/nvidia/*/bin`, иначе `cublas64_12.dll is not found`.
- CUDA-ошибки всплывают на этапе **инференса**, а не загрузки.

### Решения, принятые по качеству, а не «и так сойдёт»

- **Нечёткий поиск программ ничего не запускает.** Замеры показали, что
  верные и ложные совпадения перекрываются: «стим» → Steam даёт 0.67, и
  «chrome» → Heroes III Launcher тоже 0.67. Один порог их не разделит,
  поэтому fuzzy только предлагает варианты, а не открывает молча.
- **Абсолютная громкость измеряется.** Раньше считалось, что без COM-объекта
  аудиоэндпоинта уровень не прочитать, и навык честно отвечал «не измеряю»
  вместо случайного числа. Замер показал, что читается — см. баг 54.
  Теперь установка и сдвиг идут в процентах, а ответ называет измеренный
  итог, а не обещание.
- **Опасные действия всегда требуют подтверждения**, даже если произнесены
  один раз. Задержка 20 с перед выключением даёт время на отмену.

### Баги, найденные и исправленные по ходу

Главная ценность прогонов — эти находки неочевидны и повторятся у любого,
кто собирает такой конвейер:

1. **Кириллица в `.bat` ломала cmd.exe** — `%ERRORLEVEL%` съедался, `>nul`
   распадался, скрипт уходил в цикл ошибок. Решение: `.bat` только в ASCII.
2. **Обработчик команды был вызовом, а не ссылкой** — в словаре стояло
   `"quiet": _set_quiet(console, runtime)`, поэтому он выполнялся при
   построении словаря, то есть **на каждой строке**: озвучка выключалась
   сама. Решение: обернуть в `lambda`; закрыт регресс-тестом.
3. **Поток консоли вызывал `asyncio.create_task()` из чужого потока** —
   процесс зависал. Решение: `loop.call_soon_threadsafe`.
4. **Вывод рассинхронизировался с обработкой** — поток консоли успевал читать
   следующую строку раньше ответа на предыдущую. Решение: `Console.idle`
   как семафор; ввод блокируется до конца обработки.
5. **`AudioPlayer.play()` отменял сам себя** — сохранял вызывающую задачу,
   и `cancel()` гасил её на первом же блоке. Сессия оставалась занятой, все
   следующие реплики отбрасывались. Решение: не отменять текущую задачу.
6. **Занятая сессия проглатывала реплики молча** — пользователь видел
   «обрабатываю…» без ответа. Решение: отвечать «секунду, я отвечаю».
7. **Буква «ё» терялась дважды**: в шаблонах («сверни всё» не совпадало с
   нормализованным «сверни все») и в именах файлов («отчёт.txt»
   превращался в несуществующий «отчет.txt»). Решение: разворачивать «ё» в
   `[её]` при компиляции; для путей брать слот из исходной реплики.
8. **Склонения в шаблонах**: «окон» не совпадало с «окна», «памяти» не
   находилось. Решение: префиксные шаблоны + разбор по стеблю.
9. **`psutil.boot_time()` делили на 1000** — получалось «работает 74573543
   дня». Значение и так в секундах.
10. **`min_speech_ms` использовалось, но не сохранялось** в конструкторе.
11. Длительность фразы сравнивалась **после** очистки буфера — всегда 0.
12. Callback-поток Portaudio, в который писали через `write()` — Portaudio
    это запрещает (`PaErrorCode -9976`), звука не было вообще.
13. Откат STT на CPU терялся: `_detect_device()` заново выбирал CUDA.
14. Числа прописью не учитывали 11–19, сотни и женский род тысяч.
15. Текстовый ввод был невозможен архитектурно — FSM разрешал из `IDLE`
    только переход в `LISTENING`. Найдено тестом.
16. **`_launch_path` не искал имя в PATH, если оно уже с расширением** —
    `notepad.exe` не запускался: голое имя файла не является путём.
    Тесты этого не видели, потому что проверяли заглушку `_launch_path`,
    а не настоящую функцию. Решение: `shutil.which` проверяется всегда;
    тесты переписаны на реальную функцию с подменой лишь `os.startfile`.
17. **Строгая проверка имени отклоняла обратный слеш** — любой нормальный
    путь вида `C:\Windows\...` уходил в отказ. Решение: разделители пути
    разрешены, обход каталогов («..») запрещён явно.
18. **Перехват клавиш не был реализован вовсе.** `wake.hotkey` существовал
    только как строка в конфиге, которую печатали при старте; `pynput`
    стоял в `requirements.txt` и не импортировался ни одним модулем.
    При `require_activation = True` окно прослушивания не открывал никто,
    поэтому в голосовом режиме микрофон молчал всегда. Документация при
    этом утверждала, что `Ctrl+Space` «работает из коробки»: описание
    опередило реализацию, и 622 теста этого не замечали — нечего было
    проверять. Решение: `sinaps/ui/trigger.py` + команда `hotkeys` для
    проверки перехвата вручную.
19. **`call_soon_threadsafe` не умеет запускать корутины.** Первый вариант
    передавал в него `_run(...)`; цикл вызывал корутину и молча её
    выбрасывал — без исключения и без записи в лог приложения. Активация
    не работала, а причина не читалась ни в одном логе. Решение: задачи
    создаются через `ensure_future`, покрыто тестом с настоящим event loop.
20. **Кнопка мыши без модификатора не срабатывала никогда.** Проверка
    «если множество клавиш непустое, иначе это не триггер» отбрасывала
    пустое множество — то есть ровно те кнопки, у которых модификатора
    нет. Пустое множество означает «модификаторы не нужны».
21. **Заявленное «мышь и клавиатура работают вместе» было неправдой.**
    `build_trigger` выбирал одно: `mouse_button or hotkey`. Исправлено на
    список триггеров — иначе пришлось бы перезапускать ассистент, чтобы
    переключиться на резервный способ.
22. **Удержание подавляло обычную активацию полностью.** Пока держишь
    кнопку для разговора, клик мышью не срабатывал. Это осознанное
    решение: иначе случайный клик во время фразы запустил бы команду
    посреди сказанного.
23. **Обработчик активации вызывается с двумя аргументами, а в команде
    `hotkeys` был объявлен с одним.** Команда падала с `TypeError` прямо
    во время работы и показывала «не поймана» для кнопки, которая только
    что сработала. Решение: контракт подписи зафиксирован в docstring,
    команда проверяется тестом на сквозном вызове.
24. **Первый совпавший триггер «съедал» остальные.** Кнопка мыши без
    модификаторов совпадает при любом состоянии клавиатуры, а триггеры
    просматривались по порядку до первого совпадения. Итог: пока мышь
    зажата, сочетание клавиш никогда не срабатывало. Заодно удержание
    кнопки переоткрывало окно каждые `debounce_s` секунд. Решение:
    срабатывание по переходу «стало нажато → отпущено», а не по факту
    нажатия.
25. **Результат распознавания никто не проверял.** Whisper на тишине и
    шуме не молчит, а сочиняет правдоподобные фразы — в бою на попытку
    открыть блокнот прозвучало «Субтитры субтитров Н. Новикова» с
    `conf=0.03`. Сама уверенность считалась, логировалась и
    игнорировалась: порога не существовало. Измерено на модели `small`:
    живая речь `0.53–0.92`, выдумки `0.00–0.06`. Решение: два барьера —
    `stt.min_dbfs` до модели (дешевле, экономит ~0.5 с) и
    `stt.min_confidence` после; галлюцинация становится пустой фразой, а
    не командой.
26. **`dbfs` был продублирован в двух слоях** — `audio.capture.dbfs` и
    `vad.silero_vad.dbfs_level` были побайтово одинаковыми формулами.
    Две одинаковые реализации в разных слоях разъезжаются со временем;
    `dbfs_level` оставлен алиасом.
27. **Никто не проверял, что запущена одна копия.** На машине пользователя
    одновременно работали `run_sinaps.bat` и `run_sinaps_silent.bat`.
    Обе держали микрофон, Windows отдавала кадры попеременно, и голос
    разрывался на обрывки ровно той длины, какая успела набежать:
    «Фраза завершена: 0.98 с» дважды подряд, с одинаковой галлюцинацией.
    Симптом выглядел как поломка микрофона, и искать пришлось в железе,
    а не в запуске. Решение: `core/singleton.py` — именованный мьютекс
    `Local\Sinaps-<хэш пути проекта>`; вторая копия отказывается стартовать
    и называет PID первой. Ядро освобождает мьютекс при смерти процесса,
    поэтому залипшего состояния не бывает.
28. **Микрофон открывался ДО загрузки моделей.** Главный поток при загрузке
    Silero TTS и faster-whisper держит GIL минутами, а аудиоколбэк
    PortAudio живёт в отдельном C-потоке и без GIL не работает. Буфер
    переполнялся, кадры выбрасывались — в логе `Portaudio: input overflow`
    сразу после `Микрофон запущен`, то есть ровно тогда, когда
    пользователь проверяет звук после запуска. Решение: `Runtime.start()`
    ждёт прогрева и только потом открывает микрофон. Заодно первый запрос
    перестал платить за холодную модель. Вторая линия обороны:
    `audio.latency: high` вместо `low` (+40 мс запаса вместо ~40 мс всего).
29. **Второй процесс — это не вторая копия.** `.venv\Scripts\python.exe`
    запускает базовый интерпретатор дочерним процессом, поэтому один
    запуск даёт два PID (проверено: 3556 → 14624). Диагностика «две копии
    по числу процессов» врёт; смотреть надо на цепочки родитель-потомок.
30. **Кольцевой буфер pre-roll был мёртвым кодом.** `drain_preroll()`
    вызывался только в тестовом `record()`; в боевом прослушивании буфер
    наполнялся и никогда не подклеивался. Сегментер начинает считать лишь
    после трёх кадров подряд признанных речью — то есть начало фразы уже
    минуло, и первое слово терялось. Измерено на живых фикстурах:

        без pre-roll (3/5)      с pre-roll 500 мс (5/5)
        'Громкость громче.'      'Сделай громкость громче.'   conf 0.47 -> 0.91
        'Такая погода в Москве?' 'Какая погода в Москве?'     conf 0.81 -> 0.91
        'который час' conf 0.23  'который час' conf 0.53     (проходил порог)

    Решение: буфер забирается в момент обнаружения речи, а не в момент
    активации, и склеивается с сегментом. Перекрытия нет — кольцо
    заканчивается ровно там, где начинается сегмент.
31. **Порог `min_confidence` выведен на другой конфигурации.** Замеры
    сделаны на CPU + int8, у пользователя CUDA + int8_float16. На CUDA
    разброс тот же (0.53-0.91 против 0.00-0.06 у галлюцинаций), но
    усечённые фразы давали 0.23 — ниже порога. После починки pre-roll
    усечения исчезли, и порог снова оказался в разрыве. Вывод: пороги,
    откалиброванные по синтетике, надо проверять на боевом железе.
32. **`trim_silence` сравнивал порог с амплитудой каждого сэмпла.**
    В речи отдельные сэмплы почти всегда близки к нулю — вокализованные
    интервалы, — поэтому «амплитуда выше порога» отрезала тихое начало
    слова. На фикстурах терялось 23-36% фразы, а на живой записи с
    тихим USB-микрофоном и фоном комнаты — до 81%: добавление полусекунды
    тишины меняло итог с 1.09 с на 2.06 с. Решение: порог применяется к
    громкости ОКНА в 20 мс, отступ вырос с 50 до 150 мс. После правки фон
    перестал влиять на результат (1.24 с и conf 0.61 в обоих случаях).
33. **Падение в отладочном логе убивало весь голосовой ввод.** В лог
    обращались к несуществующему полю, исключение всплывало в `_run`,
    контур прослушивания тихо умирал, и ассистент просто замолкал. Все
    вставки диагностики в горячий путь обязаны быть безопасно падающими.
34. **`check-mic` врал при неисправном входе.** Он смотрел только на
    пиковый уровень и писал «микрофон работает», пока звук рвался, а
    потом предлагал понизить порог VAD — что при постоянном гуле только
    ухудшает дело. Замерено на реальной машине: пик −28.9 дБ, фон −29.2
    дБ, разброс громкости 0.5 дБ, VAD — 0% речи. Решение: `check-mic`
    считает долю речи по VAD и разброс громкости (90-й минус 10-й
    перцентиль). Разброс < 8 дБ означает «вход слышит только постоянный
    шум, голос не доходит» — и советует убрать громкость записи и
    поднести микрофон, а не менять порог.
35. **USB-микрофон на машине пользователя не принимает звук.** Итог
    многоступенчатой проверки, важно сохранить метод:
    1. `check-mic`: пик −28.9, фон −29.2, разброс 0.5 дБ, речи 0%.
    2. Silero в изоляции на фикстуре: вероятность 1.0, и устойчив при
       амплитуде до 0.0028 — движок исправен.
    3. Спектр записи: 19.5% энергии на 50-70 Гц, пики 49.6/50.0/50.4 Гц —
       сетевой гул, а не звук.
    4. Тон 440 Гц через колонки: прирост −0.0 дБ.
    5. Перебор всех входов (MME, DirectSound, WASAPI, WDM-KS) на 16 и
       48 кГц: разброс 0.4-0.5 дБ во всех сочетаниях. WASAPI и WDM-KS
       вообще не открываются на 16 кГц (`Invalid sample rate -9997`).
    Вывод: капсюль микрофона отключён или неисправен. Никакая настройка
    программы это не чинит — чинить надо железо. Проверка, не зависящая
    от кода: индикатор уровня в Параметры → Звук → Ввод.
36. **Захват вёлся в 16 кГц, а ни один микрофон её не поддерживает.**
    Главная находка всего разбора. Все входные устройства на этой машине:
    MME 44100, DirectSound 44100, WASAPI 48000, WDM-KS 44100. Мы просили
    PortAudio открыть поток на 16 кГц, и драйвер пережимал сам — с алиасами.
    На выходе получалось:
      * Whisper: `No speech threshold is met (0.81 > 0.60)` — считает
        вход не-речью и возвращает пустую строку;
      * Silero при этом рапортовал «речь 31% времени» — грубый детектор
        алиасы за речь принимает;
      * `trim_silence` на одном и том же входе то не резал ничего,
        то отбрасывал 75% записи.
    Противоречие «Silero слышит, а Whisper нет» — прямое следствие.
    Решение: снимаем в нативной частоте устройства и пересемплируем сами
    через `scipy.signal.resample_poly` (линейная интерполяция на сжатии
    в 2.76 раза оставляет алиасы — проверено). `blocksize` больше не
    задаётся: он измеряется в сэмплах, а частота теперь не 16 кГц.
37. **`privacy.record_audio` был объявлен и не использовался.** Ровно
    тогда, когда понадобился, флаг не помог. Подключён: фраза
    сохраняется в `data/audio/` ДО распознавания, каталог ограничен
    40 файлами. Диагностика «почему Whisper молчит» по косвенным
    признакам невозможна — нужен сам звук.
38. **Сессия застревала в THINKING навсегда.** Порядок операций был
    неверный:
    ```python
    await self.bus.emit(EventType.TRANSCRIPT_FINAL, ...)   # тут отрабатывает весь диалог
    await self.session.transition(State.THINKING, ...)     # а это уже после
    ```
    `bus.publish` ждёт обработчики, поэтому `_on_transcript` успевал
    выполнить диалог целиком и сбросить сессию в IDLE, после чего её
    снова затыкали в THINKING. Из THINKING никто не выходил, и все
    последующие активации отклонялись («сессия занята») до перезапуска.
    Решение: состояние меняется ДО публикации события. Плюс сторож,
    сбрасывающий сессию, если она в нерабочем состоянии дольше 30 с —
    обещание из документации автомата «THINKING без ответа не должен
    залипать» раньше не выполнялось, таймаут был только у LISTENING.
39. **`small` недостаточен для живого микрофона.** На чистых фикстурах
    разница невелика, на реальном голосе она проваливается:
    «Открой блокнот» -> «Открой блокмут», «Открут, открут».
    Замерено на RTX 4060 командой `evaluate`:
        small / int8_float16      уверенность 0.829
        large-v3-turbo / int8_float16   0.924
        large-v3-turbo / float16        0.934
    int8_float16 отстаёт от float16 на 0.01 и выигрывает по памяти,
    поэтому он и стал умолчанием. По умолчанию теперь `large-v3-turbo`.
40. **Хвостовая пунктуация ломала команды.** Whisper ставит точку и
    вопросительный знак в конце фразы, а шаблоны привязаны `^...$`:
        «дата»   -> ок        «Дата.»          -> не найдено
        «кто ты» -> ок        «Кто ты?»        -> не найдено
    Причём непоследовательно: «Который час?» работал — тот шаблон
    случайно оказался терпимее остальных. `normalize()` знаки внутри
    текста сохранял намеренно (нужны для «10:30», «5,5», «отчёт.txt»),
    поэтому убирать их оттуда нельзя; добавлена точечная очистка хвоста.
41. **`SkillRegistry.match()` нормализовал не полностью.** Внутри стоял
    `text.strip().lower()` вместо `normalize()`, и вызов без
    нормализации давал опасный промах: «Выключи компьютер?» не проходил
    по `^выключи компьютер$` из-за «?» и уезжал в более общий шаблон
    `apps.close` — выключение компьютера превращалось в закрытие
    приложения. Теперь `normalize()` вызывается внутри `match()`;
    повторный вызов безвреден.
42. **Эхо ассистента убивало голосовой ввод.** Микрофон слышит ответ
    ассистента, VAD честно находит в нём фразу, но сессия уже `IDLE` —
    переход `idle -> transcribing` запрещён, `InvalidTransition`
    поднимался до `_run`, а тот на ЛЮБУЮ ошибку завершался:
        Фраза завершена: 0.72 с
        Недопустимый переход idle -> transcribing
        Контур прослушивания упал
    Микрофон формально продолжал работать, голос не принимался — до
    перезапуска программы. Три защиты:
      1. фраза принимается только в состоянии `LISTENING`, остальное
         отбрасывается (это и есть эхо);
      2. `_run` ловит исключение на каждый кадр и продолжает работу,
         сбрасывая сегментер и сессию (`reset_state` — без публикации
         события, чтобы восстановление не вызвало каскад);
      3. `InvalidTransition` обрабатывается как штатная ситуация.

43. **`QSystemTrayIcon` убивал процесс без сообщения.** `qt_available()`
    вызывал `isSystemTrayAvailable()` до создания `QApplication`; Qt без
    экземпляра приложения падает наглухо — ни трассировки, ни кода
    возврата, пустой вывод и `exit 1`. Проверять Qt можно только после
    `QApplication`; до него проверяем лишь платформу и наличие пакета.

44. **Трей был единственным местом, где падал «без микрофона».**
    Консольный режим к этому относился штатно, трей — нет: `NoAudioDevice`
    уходил в трейбек и не было ни окна, ни сообщения. Теперь `_build()`
    проверяет `has_input_device()` и падает в текстовый режим с
    уведомлением. Отсутствие микрофона — не поломка, а другая
    конфигурация машины.

45. **`OSError` пролетал мимо защиты хранилища.** `_write`/`_read` ловили
    только `sqlite3.Error`, а `mkdir(parents=True)` на пути, где лежит
    файл, бросает `FileExistsError` — то есть ровно тот случай, ради
    которого защита и писалась, улетал из хранилища в голосовой контур.

46. **Окно настроек молча теряло изменения.** Сохранение делегировалось
    обратному вызову хоста: без него настройки применялись в памяти,
    окно закрывалось, а `config.yaml` оставался прежним. Заметить это
    можно через месяц. Теперь пишет само окно, `on_apply` только
    применяет «что можно без перезапуска».

47. **Неудачный автозапуск не откатывал галочку.** `on_autostart` мог
    сообщить об отказе возвратом `False`, а не исключением, — и этот путь
    галочку не трогал. Интерфейс показывал включённый автозапуск, которого
    нет.

48. **BOM (`U+FEFF`) проходил сквозь всю обработку.** `str.strip()` его не
    убирает: в Python `'\ufeff'.isspace()` ложно. Приезжал из файлов в
    UTF-8 с BOM (их пишет «Блокнот»), из конвейера PowerShell, из
    `utf-8-sig` CSV. Ломал имя файла (`'\ufeffотчёт.txt'` не существует)
    и ложался в историю невидимым мусором. Вычищается в `normalize`,
    `strip_quotes` и при записи в хранилище.

49. **История врала о себе.** `_remember` писала `latency_ms = 0` и пустое
    намерение: `Reply` от `SkillEngine` не содержал `meta`. Обмен с
    нулевой задержкой читается как мгновенный ответ. Теперь движок
    возвращает `intent`, `slots` и `score`, а задержка измеряется по
    факту.

50. **История писалась дважды.** Хост трея подписывался на
    `transcript_final` и `response_ready` и писал реплики сам — поверх
    `ConversationPipeline`. Вдобавок в базу попадали служебные реплики
    («секунду, я ещё отвечаю»), которые разговором не являются.
    Писатель должен быть один.
51. **Формы глаголов были захардкожены в двух видах.** В каждом шаблоне
    глагол перечислялся руками — «открой» и «открыть». Замер на 20
    вариантах: работало 7, не работало 13. Для человека это выглядит как
    «ассистент то понимает, то нет», хотя дело в одном пропущенном
    окончании; в живой речи «откроем» и «запущу» — норма, а не ошибка.
    Решение: `nlu/verbs.py`, 27 глаголов и 298 форм в одном месте.
    Сначала пытались выводить окончания из основы — получилось «откроу»
    и «запущуь»: у глаголов на -сти формы множественного числа идут от
    другой основы. Для распознавателя выдуманная форма хуже
    отсутствующей, поэтому формы перечислены явно.
52. **`minimize_all()` сообщал об успехе, не сделав ничего.**
    `ShellExecuteW("Minimize")` на оболочке рабочего стола больше не
    поддерживается, но возвращает ненулевой код. Замер на трёх открытых
    блокнотах: `True`, свёрнуто 0 из 3 — ассистент уверенно говорил
    «Сворачиваю всё». Решение: `WM_SYSCOMMAND` со `SC_MINIMIZE` каждому
    видимому окну — тот же замер даёт 3 из 3. `Win+D` не годится: это
    переключатель, и вторая команда подряд развернула бы окна обратно,
    а для голоса это ловушка.
53. **Громкость объявлялась в нажатиях, а двигалась в процентах.**
    Одно нажатие клавиши сдвигает уровень ровно на 2 процента
    (измерено: `GetVolumeStepInfo` отдаёт шаг 50 при 100). «Сделай
    громче» объявляло «на 5 ступеней», а громкость уезжала на 10
    процентных пунктов. Одно число — два смысла; человек слышал 5 и видел
    10 и решал, что ассистент считает неверно. Решение: единица
    измерения — проценты, и она вынесена в имя параметра
    (`volume_up(percent)`), чтобы расхождение не вернулось.
54. **Абсолютную громкость объявили нечитаемой, не проверив.**
    `volume_state()` возвращал `percent=None` со словами «без COM не
    прочитать», и навык «какая громкость» отвечал «не измеряю». Проверка
    показала: читается, через `IAudioEndpointVolume`, без сторонних
    пакетов. Решение: `platform/audio.py`. Ответ навыка теперь
    измеряется после изменения, а не вычисляется наперёд: у края
    диапазона «громче на 30» при 90 даёт не 120, а 100, и предсказание
    об этом не знает.
55. **Шаг громкости посчитали по неверной формуле.** `GetVolumeStepInfo`
    отдаёт внутренние единицы, а не проценты: внутренний диапазон — 0…10000.
    По формуле `100 * Step / StepCount` вышло 39 процентов за нажатие
    вместо двух. Обнаружено сверкой с замером. Решение: функция
    `step_size()` удалена, осталась замеренная константа с пометкой.
56. **Закрытие программ искало буквальную подстроку, а не имя.** Таблица
    русских названий была подключена только к запуску, поэтому «открой
    блокнот» срабатывало, а «закрой блокнот» — нет: `'блокнот' in
    'notepad'` ложно. В логе это читалось как «всё отработало верно»,
    запрос-то доезжал правильным — ломался перевод. Решение: общая
    таблица для обеих сторон. Заодно совпадение сделано точным: подстрока
    закрыла бы и `notepad`, и `blender` по слову «блок».
57. **Поиск предлагал выдуманные имена.** `SequenceMatcher` мерит долю
    совпавших символов, а длина в знаменателе мала: «sinaps» и «sips»
    давали 0,80 при пороге 0,72, и человек слышал `sips.pm` вместо
    ответа. Решение: требуется общее начало; настоящие опечатки проходят,
    случайные падают в ноль.
58. **Ноль считался незаполненным слотом.** `has_missing_slots` проверял
    `not match.slots.get(...)`, а ноль в Python — ложь. «Поставь громкость
    на 0» доходило до навыка с правильным числом и отвечало «нужно
    percent». Затрагивало все команды с обязательными слотами. Решение:
    проверка на `None`, закрыто тремя тестами прямо на реестре.
59. **`ShellExecuteW("Minimize")` возвращал успех, не сделав ничего.**
    Оболочка рабочего стола это слово больше не понимает, но отдаёт
    ненулевой код. Замер на трёх блокнотах: `True`, свёрнуто 0 из 3 —
    ассистент уверенно говорил «Сворачиваю всё». Решение:
    `WM_SYSCOMMAND` со `SC_MINIMIZE` каждому видимому окну, тот же замер
    даёт 3 из 3. `Win+D` не годится — это переключатель.
60. **Обещание приватности не выполнялось кодом.** `PrivacyConfig` содержал
    флаг `local_only` с комментарием «жёсткий запрет сетевых вызовов
    (требование проекта)». Проверка показала: флаг читался в двух местах,
    и оба раза **только для печати текста** — «локально, без сети» либо
    «СЕТЬ РАЗРЕШЕНА». Ничто не блокировалось; смена флага меняла надпись,
    а не поведение. Это опаснее отсутствия обещания: читатель верит и
    перестаёт проверять.
61. **Два скрытых сетевых канала, найденных измерением.**
    1. `faster-whisper`: конструктору передавался `download_root`, но не
       `local_files_only`. При удалённой модели библиотека молча шла на
       HuggingFace. Параметр в библиотеке есть — `transcribe.py:630`.
    2. `silero` TTS: `torch.hub.load(..., source="github")` — явная
       загрузка с GitHub при пустом кэше.

    Пока модели скачаны установщиком, оба канала молчат. Стоит удалить
    модель — и ядро выходит в интернет, обещав этого не делать.
    Решение: `core/offline.py`, переменные окружения до импорта движков,
    `local_files_only=True`, `source="local"`, понятная ошибка вместо
    молчаливой загрузки.
62. **Архитектурный тест на сеть ничего не ловил.** В `FORBIDDEN_NETWORK`
    лежали строки `"urllib.request"` и `"http.client"`, а проверка брала
    `name.split(".")[0]` — то есть `urllib` и `http`. Ни того, ни другого
    в наборе не было, поэтому оба импорта проходили молча: тест зелёный,
    сеть не заблокирована. То же с правилом проверки: сравнение переписано
    на префиксное по компонентам, добавлен мета-тест «проверка проверки».
63. **Первая версия проверки модели объявляла отсутствующей модель,
    которая лежала на диске.** Искала `--<имя модели>` в имени каталога,
    а huggingface_hub называет папку `models--<владелец>--faster-whisper-
    <модель>`: перед именем модели одиночный дефис, а не два. Признаком
    модели сделан файл `model.bin` внутри папки. Ассистент отказался бы
    работать с установленной моделью — ошибка выглядела бы как «модель
    не скачана», и человек пошёл бы качать заново.

### Чего сознательно нет

- **Wake-word** — самая дорогая часть проекта (см. 3.3).
- **Таймеров, напоминаний, заметок, погоды, поиска** — этап v1.
- **Яркости на десктопах** — нет подсветки. Проверено на машине
  разработчика: `Win32_Battery` пуст, а `WmiMonitorBrightnessMethods`
    содержит 0 записей — то есть тест «есть ли батарея» и реальная
    возможность менять яркость не совпадают, но поддержку ограничиваем
    ноутбуками осознанно.

Абсолютная громкость больше не в этом списке: она читается и
устанавливается точно, см. `platform/audio.py`.

### Известное ограничение окружения

На машине разработки **нет устройств ввода** (0 микрофонов, 10 выходов) и
нет интерактивной сессии рабочего стола, поэтому:
- голосовой ввод проверялся на wav-фикстурах, синтезированных тем же Silero;
- запуск программ возвращает «не удалось» — GUI недоступен из этой сессии.

Весь контур (VAD, STT, TTS, NLU, навыки, подтверждения) при этом проверен
полностью. Первое, что нужно сделать на машине с микрофоном:
`python -m sinaps check-mic`.

---

## 11. Этап 3: интерфейс (сделан)

Трей, история, настройки, автозапуск. Ассистент запускается без консоли
и им пользуются мышью, а не только голосом.

### Раскладка потоков

Qt требует главного потока, asyncio — своего. Отсюда решение:

```
главный поток   QApplication, трей, окна
рабочий поток   event loop asyncio, микрофон, модели, навыки
```

Обмен только через мосты:

* asyncio → Qt: `bridge.signal.emit(...)`. Qt сам кладёт значение в очередь
  приёмника, потому что потоки разные;
* Qt → asyncio: `loop.call_soon_threadsafe`.

У обновления окна свой мост — `_WindowBridge` в `ui/qt_host.py`, потому что
мост трея может не существовать (в тестах и там, где системного трея нет),
а обновлять окно всё равно придётся.

`QTimer.singleShot(0, ...)` из рабочего потока не годится: таймер
создаётся в том потоке, из которого вызван, и без Qt-цикла в чужом
потоке не срабатывает — молча, без ошибки и без записи в журнал. Это
стоило одного бага: история писалась в базу, а вкладка оставалась пустой.
Проверки это скрывали, потому что цикл asyncio гоняли в главном потоке,
где `QTimer` работает.

Сборку графа делает только `runtime.py` (`build_assistant`). `ui` получает
готовый объект — это проверяет `test_architecture.py`, потому что ручная
сборка в интерфейсе означала бы, что её придётся дублировать при каждом
изменении состава навыков.

### Иконка состояния рисуется кодом

`paint_icon()` создаёт `QPixmap` и рисует круг со штрихами. Не грузится из
файла: состояний девять, менять их должен разработчик без дизайнера, и
нечего таскать `.ico` каждого цвета. Qt масштабирует сам, поэтому 16 px
в трее и 64 px в настройках — один код.

Все состояния `State` обязаны быть описаны в `STATES` — это проверяет тест.
Забытое состояние иначе нарисовалось бы серым «сплю», и человек решил бы,
что ассистент не слушает.

### Хранилище истории

`memory/store.py`: `dialogues`, `turns`, `facts`, `meta`. Открывает
соединение на каждый вызов — медленнее общей копии, но не требует care'а
о том, кто его закрывает. Ошибка БД **не должна ронять разговор**, поэтому
`_write`/`_read` ловят и `sqlite3.Error`, и `OSError`: `mkdir` может
упереться в файл на месте каталога и бросить `FileExistsError`, который
иначе улетел бы прямо в голосовой контур.

История пишется из `ConversationPipeline`, а не из трея, — иначе в
консольном режиме она бы не сохранялась вовсе.

### Автозапуск

`HKCU\...\CurrentVersion\Run`, не ярлык в `shell:startup`: ярлык удаляют,
переименовывают и перетаскивают, а о нём никто не знает. Имя записи —
`Sinaps-<хэш пути>`, чтобы две копии проекта не затирали друг друга.
Путь к `pythonw.exe` абсолютный и в кавычках: относительный путь в
автозапуске разрешается от `System32`.

### Что трей не умеет

Нажатий на сами значки в Qt нет: `QSystemTrayIcon` не сообщает о клике по
иконке, только об активации области. Правая кнопка и меню работают,
левая — нет. Это ограничение API, а не недосмотр.

---

## 12. Следующий шаг

Текущая версия — **Beta 1.0**. Из плана на v1 сделано:

- ✅ **Погода** — плагин в ветке `beta-1.0`, 25 городов России и Беларуси
- ✅ **Затухание плагинов** — процесс закрывается после 2 минут простоя
- ✅ **Окно вместо трея** — Beta 1.1: разделы «Плагины», «История», «Настройки», «Справка»
- ⬜ **Дашборд состояния и онбординг** — Beta 1.2
- ⬜ **Таймеры, напоминания, заметки** — Beta 1.3
- ⬜ **Поиск, курсы, новости** — Beta 1.4
- ⬜ **LLM-fallback** на локальном Ollama для произвольных фраз — Beta 1.5

Подробный разбор по версиям — в `ТЗ Beta Sinapa.md`.

---

## 13. Плагины

Ядро ничего не знает о конкретных возможностях: оно умеет загрузить папку с
манифестом, поднять дочерний процесс и вызвать в нём команду по имени.
Всё остальное — ответственность плагина.

### Почему дочерний процесс, а не модуль

Плагин — чужой код. Исполнять его в процессе ассистента значит отдать ему
память, сокеты и все права ядра, а запрещать это потомками импорта: любой
обход — и запрет обойдён. Дочерний процесс даёт границу, которую нельзя
переписать изнутри, и стоит 4 МБ.

### Что где лежит

| Слой | Файл | Роль |
|---|---|---|
| Манифест | `plugins/manifest.py` | 5 обязательных полей, 10 известных прав |
| Поиск и загрузка | `plugins/loader.py` | дочерние процессы, протокол, затухание |
| Песочница | `plugins/sandbox.py` | запрет сетевых импортов в ребёнке |
| Одобрения | `plugins/approvals.py` | хеш прав, выдача, отзыв |
| Состояние | `plugins/state.py` | своя таблица `plugin_state` в `plugins.db` |
| Контракт | `plugins/api.py` | витрина для автора |
| Обвязка | `plugins/testing.py` | `plugin_under_test`, `run_phrase` |

### Затухание

Процесс молчит 120 с — закрывается, память освобождается; следующий запрос
поднимает его за 0.144 с. Занятые процессы уборка не трогает: `MAX_TIMEOUT_S`
(300 с) больше порога простоя, поэтому долгий вызов выглядел бы как простой.

### Метрики, события и перезагрузка

Три вещи, без которых плагинами нельзя управлять:

- **Метрики** — `plugin_stats()` считает живых, спящих и суммарный RSS.
  Идёт в журнал при загрузке и после каждой уборки, плюс в `describe()`.
  На 16 плагинах ядро растёт на 2.2 МБ — по 0.14 МБ на плагин, тогда
  как процессы занимают по 4 МБ. То есть плата за плагин идёт на ядро,
  а не на процессы.
- **События** — `PLUGIN_LOADED`, `PLUGIN_UNLOADED` (с причиной «idle»
  или «stop») и `PLUGIN_WAKE_FAILED`. Отправляет загрузчик: только он
  знает, что произошло на самом деле. Окно Beta 1.1 подписывается на них
  и не опрашивает процессы.
- **Перезагрузка** — `reload_plugin()` и `/plugins reload <имя>`. Порядок
  обязателен: сначала команды уходят из реестра, потом закрывается
  процесс, потом грузится новый. Обратный порядок оставляет промежуток,
  в который команда ещё в реестре, а нового процесса ещё нет.

### Проверки, а не обещания

У системы плагинов есть проверки-нарушители: каждая роняет тесты, если
защиту убрать. Отдельно проверяется, что запрет сети действительно не
проходится, что права проверяются **до** похода в базу, что уборка
подключена в рантайме, а не только существует в загрузчике, и что при
16 плагинах ядро не растёт.

Про набор проверок: из шести врезок с первого раза падали только три.
Две проходили насквозь, потому что тесты подставляли отчёт руками и обрыв
в сборщике рантайма не задевали; одна не упала, а овесила прогон. Все три
исчезли после переделки проверок — но только потому, что врезки
выполнялись по-настоящему, а не «проглядывались глазом».

Руководство автора — `docs/PLUGINS.md`.
