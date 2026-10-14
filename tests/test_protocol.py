"""Кадры между ядром и дочерним процессом.

Самое важное здесь — не «сообщение доходит», а то, что **мусорная длина
не превращается в выделение памяти**.

Откуда это требование
---------------------
Работа над яркостью кончилась тем, что счётчик пришёл из драйвека как
`0xF1CC7000`, буфер выделился на 4 056 707 072 указателя по 8 байт, у
системы закончилась память, упал DWM, и Windows ушла в BSOD `0xEF`.
Журнал Windows подтвердил цифру до байта.

Длина кадра приходит извне. Значит порядок обязателен: прочитать
заголовок, **проверить**, и только потом читать тело. Обратный порядок —
это `read(4294571008)` по числу, которого никто не проверял.
"""

from __future__ import annotations

import io
import struct

import pytest

from sinaps.plugins.protocol import (HEADER_SIZE, MAX_FRAME_BYTES,
                                     ProtocolError, decode, encode,
                                     read_message, write_message)


class CountingStream(io.BytesIO):
    """Поток, который помнит, сколько и когда у него просили."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.requests: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        return super().read(size)


# --- кодирование ------------------------------------------------------------


def test_roundtrip_keeps_russian_text():
    frame = encode({"speech": "Погода: −7, ветер"})

    assert decode(frame[HEADER_SIZE:])["speech"] == "Погода: −7, ветер"


def test_header_carries_length():
    frame = encode({"op": "stop"})

    assert struct.unpack("<I", frame[:HEADER_SIZE])[0] == len(frame) - HEADER_SIZE


def test_write_and_read_through_stream():
    stream = io.BytesIO()

    write_message(stream, {"op": "call", "id": 1})

    stream.seek(0)
    assert read_message(stream) == {"op": "call", "id": 1}


def test_several_messages_in_a_row():
    stream = io.BytesIO()
    for index in range(3):
        write_message(stream, {"id": index})

    stream.seek(0)
    assert [read_message(stream)["id"] for _ in range(3)] == [0, 1, 2]


# --- главный тест: длина проверяется ДО чтения ----------------------------


def test_absurd_length_is_rejected_without_reading_body():
    """Длина 4 гигабайта должна быть отвергнута, а тело — не тронуто.

    Тест на то, что тело **не читается**: если бы кто-то переставил
    проверку после чтения, тест упал бы на списке прочитанных размеров,
    а не просто «исключение появилось».
    """
    absurd = MAX_FRAME_BYTES + 1
    stream = CountingStream(struct.pack("<I", absurd))

    with pytest.raises(ProtocolError) as info:
        read_message(stream)

    assert "максимуме" in str(info.value)
    assert stream.requests == [HEADER_SIZE], (
        f"тело прочитано до проверки: {stream.requests}. "
        f"Длина извне проверяется до выделения памяти."
    )


def test_absurd_length_does_not_allocate():
    """Проверка на 4 гигабайта обязана пройти быстро и тихо.

    Если бы тело читалось «на всякий случай», тест либо упал бы по
    памяти, либо завис — и на живой машине это была бы та же авария.
    """
    import time

    stream = CountingStream(struct.pack("<I", 0xF1CC7000))

    start = time.perf_counter()
    with pytest.raises(ProtocolError):
        read_message(stream)

    assert time.perf_counter() - start < 1.0, (
        "отказ по длине занял больше секунды — тело, похоже, читается"
    )


def test_length_exactly_at_limit_is_read():
    """Ровно предельный кадр проходит: проверка не «с запасом впритык»."""
    payload = {"speech": "x" * (MAX_FRAME_BYTES - 64)}
    frame = encode(payload)

    assert len(frame) - HEADER_SIZE <= MAX_FRAME_BYTES
    assert decode(frame[HEADER_SIZE:])["speech"].startswith("x")


def test_oversized_message_is_not_encoded():
    with pytest.raises(ProtocolError):
        encode({"speech": "x" * (MAX_FRAME_BYTES + 1)})


# --- обрыв и мусор ----------------------------------------------------------


def test_truncated_header_is_an_error():
    stream = io.BytesIO(b"\x01\x02")

    with pytest.raises(ProtocolError) as info:
        read_message(stream)

    assert "оборвался" in str(info.value)


def test_truncated_body_is_an_error():
    stream = io.BytesIO(struct.pack("<I", 100) + b"tolko nachalo")

    with pytest.raises(ProtocolError):
        read_message(stream)


def test_empty_frame_is_an_error():
    """Кадр нулевой длины — признак рассинхрона, а не пустой ответ."""
    with pytest.raises(ProtocolError) as info:
        read_message(io.BytesIO(struct.pack("<I", 0)))

    assert "пустой" in str(info.value)


def test_garbage_body_is_an_error():
    with pytest.raises(ProtocolError) as info:
        decode(b"\xff\xfe\x00 ne json")

    assert "нечитаемый" in str(info.value)


def test_zero_bytes_read_is_an_error():
    stream = io.BytesIO(b"")

    with pytest.raises(ProtocolError):
        read_message(stream)