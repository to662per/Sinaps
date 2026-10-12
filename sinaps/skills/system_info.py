"""Навыки состояния системы и питания.

Питание — единственная группа опасных действий на этом этапе: выключение
компьютера голосом без спроса недопустимо. Поэтому эти команды помечены
`dangerous=True`, и конвейер требует подтверждения перед выполнением.
"""

from __future__ import annotations

from ..skills.base import Response, Skill, SlotSpec

_PARAM_ALIASES = {
    # Именительный и родительный падеж: «память» и «сколько памяти».
    "процессор": "cpu", "процессора": "cpu", "цп": "cpu", "cpu": "cpu",
    "память": "memory", "памяти": "memory", "оперативку": "memory",
    "оперативная память": "memory", "оперативной памяти": "memory",
    "ram": "memory", "озу": "memory",
    "диск": "disk", "диска": "disk", "диски": "disk", "накопитель": "disk",
    "батарея": "battery", "батареи": "battery", "аккумулятор": "battery",
    "сеть": "network", "интернет": "network", "интернета": "network",
}


def register() -> Skill:
    skill = Skill("system_info", description="Состояние системы")

    async def status(ctx, slots):  # noqa: ANN001
        info = ctx.platform.status()
        parts = []
        if info.cpu_percent is not None:
            parts.append(f"процессор {_round(info.cpu_percent)} процентов")
        if info.memory_percent is not None:
            used = f" ({info.memory_used_gb} из {info.memory_total_gb} гигабайт)" \
                if info.memory_used_gb else ""
            parts.append(f"память {_round(info.memory_percent)} процентов{used}")
        if info.disk_percent is not None:
            free = f", свободно {info.disk_free_gb} гигабайт" if info.disk_free_gb else ""
            parts.append(f"диск {_round(info.disk_percent)} процентов{free}")
        if info.battery_percent is not None:
            plug = "заряжается" if info.plugged else "на батарее"
            parts.append(f"батарея {_round(info.battery_percent)} процентов, {plug}")
        if info.uptime_hours is not None:
            parts.append(f"работает {_uptime(info.uptime_hours)}")

        if not parts:
            return Response(speech="Не смог получить данные о системе")
        return Response(speech="Состояние системы: " + ", ".join(parts),
                        display="; ".join(parts))

    async def one_param(ctx, slots):  # noqa: ANN001
        param = slots["param"]
        info = ctx.platform.status()
        text = _describe(param, info)
        return Response(speech=text) if text else Response(
            speech=f"Не знаю, что такое «{param}»"
        )

    skill.command(
        "status",
        status,
        patterns=[
            r"^(?:статус системы|состояние системы|как дела с компьютером|как система)$",
            r"^покажи состояние системы$",
            r"^system status$",
        ],
        examples=["статус системы", "как дела с компьютером"],
        description="Общая сводка о системе",
        priority=10,
    )

    skill.command(
        "param",
        one_param,
        patterns=[
            # «сколько памяти», «загрузка процессора», «свободно на диске».
            # Порядок важен: сначала самые длинные формулировки.
            r"^(?:сколько\s+)?(?:свободно\s+)?(?:на\s+)?"
            r"(?P<param>оперативн\w*\s+памят\w+|памят\w+|процессор\w*|диск\w*|"
            r"накопител\w*|батаре\w*|аккумулятор\w*|интернет\w*|сеть)$",
            r"^(?:загрузка|загруженность|состояние)\s+(?P<param>процессор\w*)$",
            r"^(?:заряд|заряда)\s+(?P<param>батаре\w*)$",
            r"^какая\s+(?P<param>памят\w*)$",
        ],
        examples=["сколько памяти", "загрузка процессора",
                  "сколько свободно на диске", "заряд батареи"],
        slots={"param": SlotSpec("param", "string", min_length=2,
                                 description="Параметр системы")},
        description="Отдельный параметр системы",
        priority=12,
    )

    return skill


def _describe(param: str, info) -> str:
    key = _resolve_param(param)
    if key == "cpu" and info.cpu_percent is not None:
        return f"Процессор загружен на {_round(info.cpu_percent)} процентов"
    if key == "memory" and info.memory_percent is not None:
        free = ""
        if info.memory_used_gb and info.memory_total_gb:
            free = f", занято {info.memory_used_gb} из {info.memory_total_gb} гигабайт"
        return f"Память загружена на {_round(info.memory_percent)} процентов{free}"
    if key == "disk" and info.disk_percent is not None:
        free = f", свободно {info.disk_free_gb} гигабайт" if info.disk_free_gb else ""
        return f"Диск заполнен на {_round(info.disk_percent)} процентов{free}"
    if key == "battery" and info.battery_percent is not None:
        return f"Заряд батареи {_round(info.battery_percent)} процентов"
    return ""


def _resolve_param(param: str) -> str:
    """Сопоставляет параметр с ключом, понимая склонения.

    Нужен префиксный разбор: «памяти», «память», «памятю» должны вести
    к одному ключу, а не искаться в словаре дословно.
    """
    word = _norm_word(param)
    if word in _PARAM_ALIASES:
        return _PARAM_ALIASES[word]

    # Отсекаем окончание и пробуем снова: «памяти» -> «памят» -> «память».
    for cut in range(len(word) - 1, 2, -1):
        stem = word[:cut]
        for alias, key in _PARAM_ALIASES.items():
            if alias.startswith(stem) or stem.startswith(alias):
                return key
    return ""


def _norm_word(text: str) -> str:
    return (text or "").lower().replace("ё", "е").strip()


def _round(value: float) -> int:
    return round(value)


def _uptime(hours: float) -> str:
    from ..nlu.slots import _plural

    total_minutes = int(hours * 60)
    days, rest = divmod(total_minutes, 24 * 60)
    hrs, minutes = divmod(rest, 60)

    parts = []
    if days:
        parts.append(_plural(days, "день", "дня", "дней"))
    if hrs:
        parts.append(_plural(hrs, "час", "часа", "часов"))
    if minutes and not days:
        parts.append(_plural(minutes, "минута", "минуты", "минут"))
    return " ".join(parts) or "меньше минуты"
