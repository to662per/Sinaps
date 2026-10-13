"""Навыки питания — требуют подтверждения.

Единственное место, где ассистент может физически помешать пользователю.
Каждая команда помечена `dangerous=True`: конвейер спросит «подтверди: да или
нет» и выполнит действие только после явного согласия.

Задержка перед выключением тоже важна: пользователь должен успеть отменить.
"""

from __future__ import annotations

from ..nlu.verbs import verb
from ..platform.base import PowerAction
from ..skills.base import Response, Skill

#: Сколько секунд даём на отмену перед выключением.
SHUTDOWN_DELAY = 20


def register() -> Skill:
    skill = Skill("power", description="Питание")

    async def _do(ctx, action: PowerAction, speech: str) -> Response:  # noqa: ANN001
        if action not in ctx.platform.supported_power:
            return Response(speech=f"Действие «{speech}» недоступно")

        delay = SHUTDOWN_DELAY if action in (
            PowerAction.SHUTDOWN, PowerAction.RESTART) else 0
        ok = ctx.platform.power_action(action, delay_s=delay)
        if not ok:
            return Response(speech="Не получилось выполнить команду питания")

        if delay:
            return Response(
                speech=f"{speech}. Отмена командой «отмена»",
                display=f"{action.value}, задержка {delay} с",
            )
        return Response(speech=f"{speech}", display=action.value)

    async def shutdown(ctx, slots):  # noqa: ANN001
        return await _do(ctx, PowerAction.SHUTDOWN, "Выключаю компьютер")

    async def restart(ctx, slots):  # noqa: ANN001
        return await _do(ctx, PowerAction.RESTART, "Перезагружаю компьютер")

    async def sleep(ctx, slots):  # noqa: ANN001
        return await _do(ctx, PowerAction.SLEEP, "Усыпляю компьютер")

    async def lock(ctx, slots):  # noqa: ANN001
        return await _do(ctx, PowerAction.LOCK, "Блокирую экран")

    skill.command(
        "shutdown",
        shutdown,
        patterns=[
            rf"^(?:{verb('выключить')}|shutdown|заверши работу)\s*"
            r"(?:компьютер|комп|ноутбук|пк|всё|все)?$",
            r"^погаси компьютер$",
        ],
        examples=["выключи компьютер", "заверши работу"],
        dangerous=True,
        description="Выключить компьютер",
        priority=10,
    )

    skill.command(
        "restart",
        restart,
        patterns=[
            rf"^(?:{verb('перезагрузить')}|restart)\s*(?:компьютер|комп|ноутбук|пк|всё|все)?$",
        ],
        examples=["перезагрузи компьютер", "перезапусти всё"],
        dangerous=True,
        description="Перезагрузить компьютер",
        priority=10,
    )

    skill.command(
        "sleep",
        sleep,
        patterns=[
            rf"^(?:{verb('усыпить')}|спящий режим|сон)\s*(?:компьютер|комп|ноутбук|всё|все)?$",
            r"^sleep$",
        ],
        examples=["усыпи компьютер", "спящий режим"],
        dangerous=True,
        description="Перевести компьютер в спящий режим",
        priority=10,
    )

    skill.command(
        "lock",
        lock,
        patterns=[
            rf"^(?:{verb('заблокировать')})\s*(?:экран|компьютер|комп)?$",
            r"^lock$",
        ],
        examples=["заблокируй экран", "запри компьютер"],
        dangerous=True,
        description="Заблокировать экран",
        priority=10,
    )

    return skill
