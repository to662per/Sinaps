"""Абсолютная громкость в Windows через COM, без сторонних пакетов.

Зачем
----
Раньше `volume_state()` возвращал `percent=None`: считалось, что без COM
абсолютный уровень не прочитать, и навык «какая громкость» отвечал
правдой только про «уровень неизвестен». Это было честно, но unhelpful:
пользователь, который говорит «сделай громче на 10», ждёт десяти
процентов, а получал вслепую нажатия по клавише.

Что здесь лежит
---------------
Обёртка над `IAudioEndpointVolume`: чтение уровня, чтение и установка
приглушения, установка точного процента.

Почему COM, а не `keybd_event`
------------------------------
Клавиша громкости двигает уровень дискретно — на 2 процента за нажатие
(замерено: `GetVolumeStepInfo` отдаёт шаг 50 при 100, одно нажатие даёт
ровно +2.0). Дискретность не мешает, когда нужно просто «громче», но
мешает, когда нужно «точно на 10 процентов»: округление накапливается,
и фактический уровень уезжает.

COM живёт в потоке. Без `CoInitialize` в этом модуле `CoCreateInstance`
отдаёт `CO_E_NOTINITIALIZED` и громкость не читается вовсе — такая
попытка уже была, поэтому инициализация вынесена на уровень модуля.
"""

from __future__ import annotations

import ctypes
from ctypes import (POINTER, c_float, c_int, c_long, c_ubyte, c_uint,
                    c_ulong, c_ushort, c_void_p)

# CLSCTX_ALL — искать объект во всех квартирах.
_CLSCTX_ALL = 23
_E_RENDER = 0
_E_MULTIMEDIA = 1


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    text = text.strip("{}").replace("-", "")
    out = _GUID()
    out.Data1 = int(text[0:8], 16)
    out.Data2 = int(text[8:12], 16)
    out.Data3 = int(text[12:16], 16)
    for i in range(8):
        out.Data4[i] = int(text[16 + i * 2:18 + i * 2], 16)
    return out


_CLSID_ENUMERATOR = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_ENUMERATOR = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_IID_ENDPOINT_VOLUME = _guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")

_ole32 = ctypes.windll.ole32
_ole32.CoCreateInstance.argtypes = [
    POINTER(_GUID), c_void_p, c_uint, POINTER(_GUID), POINTER(c_void_p),
]
_ole32.CoCreateInstance.restype = c_long
_ole32.CoInitialize.argtypes = [c_void_p]
_ole32.CoInitialize.restype = c_long

#: Инициализация нужна в каждом потоке, который читает громкость.
#: Возвращает S_FALSE или RPC_E_CHANGED_MODE, если апартамент уже был
#: создан — это не ошибка, поэтому результат не проверяем.
_ole32.CoInitialize(None)


class _IAudioEndpointVolume(ctypes.Structure):
    pass


def _method(obj, index: int, *argtypes):
    """Метод интерфейса по номеру в таблице виртуальных вызовов.

    Нумерация начинается с нуля: 0 — `QueryInterface`, 1 — `AddRef`,
    2 — `Release`, дальше собственные методы интерфейса.
    """
    table = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(table[index])


#: Индексы нужных методов IAudioEndpointVolume.
_M_GET_SCALAR = 9    # GetMasterVolumeLevelScalar(float*)
_M_SET_SCALAR = 7    # SetMasterVolumeLevelScalar(float)
_M_GET_MUTE = 15     # GetMute(int*, GUID*)
_M_SET_MUTE = 14     # SetMute(int, GUID*)


def _endpoint():
    """Указатель на IAudioEndpointVolume устройства вывода по умолчанию.

    Возвращает `None`, если устройства нет (например, звук отключён
    в системе) — тогда вызывающий переходит на клавиши.
    """
    enumerator = c_void_p()
    if _ole32.CoCreateInstance(ctypes.byref(_CLSID_ENUMERATOR), None,
                               _CLSCTX_ALL, ctypes.byref(_IID_ENUMERATOR),
                               ctypes.byref(enumerator)) != 0:
        return None
    if not enumerator.value:
        return None

    device = POINTER(c_void_p)()
    get_default = _method(enumerator, 4, c_int, c_int,
                          POINTER(POINTER(c_void_p)))
    if get_default(enumerator, _E_RENDER, _E_MULTIMEDIA,
                   ctypes.byref(device)) != 0:
        return None

    volume = POINTER(_IAudioEndpointVolume)()
    activate = _method(device, 3, POINTER(_GUID), c_uint,
                       POINTER(c_void_p), POINTER(c_void_p))
    if activate(device, ctypes.byref(_IID_ENDPOINT_VOLUME), _CLSCTX_ALL,
                None, ctypes.cast(ctypes.byref(volume), POINTER(c_void_p))) != 0:
        return None

    return volume if volume else None


def read() -> tuple[int, bool] | None:
    """Текущий уровень в процентах и признак приглушения.

    `None`, если прочитать не удалось. Второе число при ошибке не
    выдумывается: вызывающий обязан это учесть.
    """
    volume = _endpoint()
    if volume is None:
        return None

    level = c_float()
    get = _method(volume, _M_GET_SCALAR, POINTER(c_float))
    if get(volume, ctypes.byref(level)) != 0:
        return None

    muted = c_int()
    get_mute = _method(volume, _M_GET_MUTE, POINTER(c_int), POINTER(c_void_p))
    get_mute(volume, ctypes.byref(muted), None)

    # Уровень округляем: в речи дробная часть («сорок семь и три
    # десятых процента») невозможна, а на границе 49.5 наверх ползёт
    # разными путями и число «скачет» при повторных запросах.
    return max(0, min(100, round(level.value * 100))), bool(muted.value)


def set_percent(percent: int) -> bool:
    """Поставить точный уровень. `False`, если нет устройства вывода."""
    volume = _endpoint()
    if volume is None:
        return False
    setter = _method(volume, _M_SET_SCALAR, c_float)
    value = max(0, min(100, int(percent)))
    return setter(volume, c_float(value / 100.0)) == 0


def set_muted(muted: bool) -> bool:
    volume = _endpoint()
    if volume is None:
        return False
    setter = _method(volume, _M_SET_MUTE, c_int, POINTER(c_void_p))
    return setter(volume, c_int(1 if muted else 0), None) == 0


def step_size() -> int | None:
    """Размер шага клавиши громкости в процентах.

    Через `GetVolumeStepInfo` это вычисляется как `100 * Step /
    StepCount`, но формула неверна: внутренний диапазон Windows —
    0…10000, а не 0…100. На этой машине API отдал 50 и 51, то есть
    по этой формуле 39 процентов за нажатие, тогда как замер показал
    ровно 2.

    Поэтому функция не выдумывает: возвращает `None`, и вызывающий
    берёт замеренную константу. Лишний метод убран бы, если бы не
    соблазннить кого-то ещё той же формулой.
    """
    return None