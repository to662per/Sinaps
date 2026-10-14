"""Типизированные ошибки.

Правило: скиллы и движки не бросают «просто Exception» в ядро — они бросают
подкласс `SinapsError`. Иначе оркестратор не сможет отличить «не понял
команду» от «упал микрофон» и даст пользователю неверный ответ.
"""

from __future__ import annotations


class SinapsError(Exception):
    """Базовая ошибка Sinaps. `speech` — что сказать пользователю вслух."""

    def __init__(self, message: str, *, speech: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.speech = speech or message


# --- Конфигурация и запуск --------------------------------------------------


class ConfigError(SinapsError):
    """Некорректный или отсутствующий параметр конфигурации."""


class EngineUnavailable(SinapsError):
    """Движок (STT/TTS/VAD/LLM) не установлен или не загрузился."""

    def __init__(self, engine: str, hint: str = "") -> None:
        super().__init__(
            f"Движок '{engine}' недоступен",
            speech="Эта функция сейчас недоступна, извини",
        )
        self.engine = engine
        self.hint = hint


# --- Аудио ------------------------------------------------------------------


class AudioError(SinapsError):
    """Ошибка ввода/вывода звука."""


class NoAudioDevice(AudioError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            f"Микрофон не найден. {detail}".strip(),
            speech="Я не слышу тебя, проверь микрофон",
        )


# --- Речь -------------------------------------------------------------------


class SpeechError(SinapsError):
    """Ошибка распознавания или синтеза речи."""


class NotUnderstood(SpeechError):
    """Речь распознана, но команд не найдено."""

    def __init__(self, text: str = "") -> None:
        super().__init__(
            f"Команда не распознана: {text!r}" if text else "Команда не распознана",
            speech="Не понял, попробуй сказать иначе",
        )
        self.text = text


class TranscriptEmpty(SpeechError):
    """Пользователь молчал или сказал слишком тихо."""

    def __init__(self) -> None:
        super().__init__("Пустая расшифровка", speech="Я ничего не услышал")


# --- Навыки -----------------------------------------------------------------


class SkillError(SinapsError):
    """Навык не смог выполнить команду. `speech` уже готов для озвучки."""

    def __init__(self, message: str, *, speech: str | None = None) -> None:
        super().__init__(message, speech=speech)


class SkillNotFound(SkillError):
    def __init__(self, intent: str) -> None:
        super().__init__(f"Навык для намерения '{intent}' не найден")


class DangerousActionDenied(SkillError):
    """Пользователь отказался от опасного действия."""

    def __init__(self) -> None:
        super().__init__("Действие отменено пользователем", speech="Отмено")


class InvalidSlot(SkillError):
    """Слот не прошёл валидацию."""

    def __init__(self, slot: str, value: object, reason: str = "") -> None:
        detail = f" ({reason})" if reason else ""
        super().__init__(
            f"Некорректное значение слота '{slot}'={value!r}{detail}",
            speech="Не понял, что именно ты хочешь",
        )
        self.slot = slot
        self.value = value


# --- Навыки: инфраструктура ------------------------------------------------


class PluginError(SinapsError):
    """Ошибка загрузки или исполнения плагина."""


class NetworkDisabled(PluginError):
    """Попытка сетевого вызова при режиме «строго локально»."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"Сетевой доступ запрещён: {what}",
            speech="Эта функция сейчас недоступна: нет доступа к сети",
        )
        self.what = what


class PermissionDenied(PluginError):
    """Плагин позвал то, на что не получил разрешения.

    Отдельный тип от `NetworkDisabled`, потому что сеть — частный
    случай, а дальше будут файлы, буфер, уведомления. Разница видна
    человеку: про сеть сказано «нет доступа к сети», про остальное —
    что нужно одобрение в панели.

    Текст в лог и произносимое вслух — разные вещи: вслух человек не
    должен получать внутренние названия.
    """

    def __init__(self, permission: str, plugin: str = "") -> None:
        who = f"Плагин «{plugin}»" if plugin else "Плагин"
        super().__init__(
            f"{who}: нет одобрения на право «{permission}»",
            speech=("Эта функция сейчас недоступна: плагину нужно "
                    "разрешение, которого пока нет"),
        )
        self.permission = permission
        self.plugin = plugin
