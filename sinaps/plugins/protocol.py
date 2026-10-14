"""Разговор ядра с дочерним процессом плагина.

Почему дочерний процесс, а не модуль в том же интерпретаторе
-----------------------------------------------------------
В том же процессе запретить сети нельзя. `enforce_offline()` выставляет
`HF_HUB_OFFLINE` и подобные переменные, но `urllib.request` их не читает:
импортировал — и пошёл. Значит разрешение `network.http` было бы
неисполняемым обещанием, а обещание без исполнения в этом проекте уже
стоило дорого дважды.

Отдельный процесс даёт то, чего не даёт модуль:

  * код плагина физически не выполняется в процессе ассистента;
  * зависший плагин можно убить, ассистент при этом жив;
  * мусор из плагина не попадает в память ядра.

Кадры
-----
Сообщения — JSON в кадре: четыре байта длины (little-endian, без
знака) и сам JSON в UTF-8. Отдельная длина нужна, потому что `readline`
по `\n` ломается на pretty-printed JSON, а читать «сколько пришло» без
границы — значит читать чужое число и выделять под него память.

**Проверка длины обязана предшествовать чтению.** Ровно этим кончилась
работа над яркостью: `count` пришёл из драйвера как мусорные
`0xF1CC7000`, буфер выделился на 4 056 707 072 указателя, закончилась
память, упал DWM, система ушла в BSOD `0xEF`. Длина извне проверяется
до `readexactly`, а не после.
"""

from __future__ import annotations

import json
import struct
from typing import Any

#: Больше этого кадр быть не может — читаем и отвергаем, не выделяя.
#:
#: 8 МБ — с большим запасом для ответа с текстом погоды на неделю
#: вперёд, и мало для того, чтобы случайное число разорило процесс.
MAX_FRAME_BYTES = 8 * 1024 * 1024

_HEADER = struct.Struct("<I")
HEADER_SIZE = _HEADER.size

#: Операции от ядра к плагину.
OP_HELLO = "hello"
OP_CALL = "call"
OP_STOP = "stop"

#: Операции от плагина к ядру.
OP_READY = "ready"
OP_RESULT = "result"
OP_FAILED = "failed"


class ProtocolError(Exception):
    """Кадр не читается: мусор, обрыв трубы или слишком большой размер."""


def encode(message: dict[str, Any]) -> bytes:
    """Упаковать сообщение в кадр."""
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"сообщение {len(payload)} байт, максимум {MAX_FRAME_BYTES}"
        )
    return _HEADER.pack(len(payload)) + payload


def _read_exactly(stream, size: int) -> bytes:
    """Прочитать ровно `size` байт или честно сказать, что не вышло."""
    chunks = []
    left = size
    while left:
        chunk = stream.read(left)
        if not chunk:
            raise ProtocolError(
                f"поток оборвался: ждали {size} байт, получили {size - left}"
            )
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def decode(raw: bytes) -> dict[str, Any]:
    """Распаковать кадр. Битый JSON — понятная ошибка, а не исключение
    где-то в глубине разбора."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"нечитаемый кадр: {exc}") from exc


def read_message(stream) -> dict[str, Any]:
    """Прочитать кадр из потока.

    Порядок именно такой: сначала длина, потом проверка, потом чтение.
    Обратный порядок — это выделение памяти по числу извне.
    """
    header = _read_exactly(stream, HEADER_SIZE)
    (size,) = _HEADER.unpack(header)
    if size == 0:
        raise ProtocolError("пустой кадр")
    if size > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"кадр {size} байт при максимуме {MAX_FRAME_BYTES}"
        )
    return decode(_read_exactly(stream, size))


def write_message(stream, message: dict[str, Any]) -> None:
    """Записать кадр в поток и смыть."""
    stream.write(encode(message))
    stream.flush()


async def read_message_async(reader) -> dict[str, Any]:
    """То же для асинхронного потока ядра."""
    header = await reader.readexactly(HEADER_SIZE)
    (size,) = _HEADER.unpack(header)
    if size == 0:
        raise ProtocolError("пустой кадр")
    if size > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"кадр {size} байт при максимуме {MAX_FRAME_BYTES}"
        )
    return decode(await reader.readexactly(size))


async def write_message_async(writer, message: dict[str, Any]) -> None:
    writer.write(encode(message))
    await writer.drain()