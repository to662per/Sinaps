# -*- coding: utf-8 -*-
"""Погода через Open-Meteo (бесплатно, без ключа).

API: https://open-meteo.com — не требует ключа, работает по HTTPS.

Примеры:
  - «погода в Москве»
  - «какая погода завтра в Питере»
  - «прогноз на неделю в Новосибирске»
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from sinaps.plugins.api import Response, Skill, SlotSpec


# Города с координатами. Ключи — в именительном и предложном падеже.
CITIES = {
    "москва": (55.7558, 37.6176),
    "москве": (55.7558, 37.6176),
    "спб": (59.9343, 30.3351),
    "питер": (59.9343, 30.3351),
    "питере": (59.9343, 30.3351),
    "санкт-петербург": (59.9343, 30.3351),
    "петербурге": (59.9343, 30.3351),
    "новосибирск": (55.0084, 82.9357),
    "новосибирске": (55.0084, 82.9357),
    "екатеринбург": (56.8389, 60.6057),
    "екатеринбурге": (56.8389, 60.6057),
    "казань": (55.7887, 49.1221),
    "казани": (55.7887, 49.1221),
    "нижний новгород": (56.3269, 44.0075),
    "нижнем новгороде": (56.3269, 44.0075),
    "челябинск": (55.1599, 61.4026),
    "челябинске": (55.1599, 61.4026),
    "самара": (53.1959, 50.1015),
    "самаре": (53.1959, 50.1015),
    "омск": (54.9885, 73.3242),
    "омске": (54.9885, 73.3242),
    "ростов": (47.2357, 39.7015),
    "ростоке": (47.2357, 39.7015),
    "уфа": (54.7388, 55.9721),
    "уфе": (54.7388, 55.9721),
    "красноярск": (56.0184, 92.8672),
    "красноярске": (56.0184, 92.8672),
    "воронеж": (51.6720, 39.1843),
    "воронеже": (51.6720, 39.1843),
    "пермь": (58.0105, 56.2502),
    "перми": (58.0105, 56.2502),
    "volgograd": (48.7080, 44.5133),
    "волгограде": (48.7080, 44.5133),
    "краснодар": (45.0393, 38.9872),
    "краснодаре": (45.0393, 38.9872),
    "саратов": (51.5336, 46.0343),
    "саратове": (51.5336, 46.0343),
    "тюмень": (57.1522, 65.5272),
    "тюмени": (57.1522, 65.5272),
    "минск": (53.9045, 27.5615),
    "минске": (53.9045, 27.5615),
    "киев": (50.4501, 30.5234),
    "киеве": (50.4501, 30.5234),
}


async def _fetch_weather(lat: float, lon: float, days: int = 3) -> dict[str, Any]:
    """Запрос к Open-Meteo.

    Импорт urllib внутри функции — чтобы песочница не блокировала
    загрузку модуля плагина на этапе импорта.
    """
    import urllib.request

    base = "https://api.open-meteo.com/v1/forecast"
    params = (
        f"latitude={lat}&longitude={lon}"
        f"&current_weather=true"
        f"&daily=temperature_2m_max,temperature_2m_min,weathercode"
        f"&timezone=auto"
        f"&forecast_days={days}"
    )
    url = f"{base}?{params}"

    def do_request():
        req = urllib.request.Request(url, headers={"User-Agent": "Sinaps/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return await asyncio.to_thread(do_request)


def _decode_code(code: int) -> str:
    codes = {
        0: "ясно",
        1: "преимущественно ясно",
        2: "переменная облачность",
        3: "пасмурно",
        45: "туман",
        48: "туман с инеем",
        51: "морось слабая",
        53: "морось",
        55: "морось сильная",
        56: "ледяная морось слабая",
        57: "ледяная морось",
        61: "дождь слабый",
        63: "дождь",
        65: "дождь сильный",
        66: "ледяной дождь слабый",
        67: "ледяной дождь",
        71: "снег слабый",
        73: "снег",
        75: "снег сильный",
        77: "снежные зерна",
        80: "ливневики слабые",
        81: "ливневики",
        82: "ливневики сильные",
        85: "снегопад слабый",
        86: "снегопад",
        95: "гроза",
        96: "гроза с градом",
        99: "гроза с сильным градом",
    }
    return codes.get(code, f"код {code}")


async def _current(ctx, slots) -> Response:
    city = slots.get("city", "").lower().strip()
    if not city:
        return Response(speech="Назови город, например: погода в Москве")

    coords = CITIES.get(city)
    if not coords:
        return Response(speech=f"Не знаю город «{city}». Напиши название крупнее.")

    lat, lon = coords
    try:
        data = await _fetch_weather(lat, lon, days=1)
    except Exception as exc:
        return Response(speech=f"Не удалось получить погоду: {exc}")

    cw = data.get("current_weather", {})
    temp = cw.get("temperature")
    wind = cw.get("windspeed")
    code = cw.get("weathercode")
    desc = _decode_code(code) if code is not None else "неизвестно"

    city_name = city.capitalize()
    return Response(
        speech=f"{city_name}: {temp}°C, {desc}, ветер {wind} м/с",
        display=f"{city_name}: {temp}°C, {desc}, ветер {wind} м/с",
    )


async def _forecast(ctx, slots) -> Response:
    city = slots.get("city", "").lower().strip()
    when = slots.get("when", "").lower().strip()

    if not city:
        return Response(speech="Назови город")

    coords = CITIES.get(city)
    if not coords:
        return Response(speech=f"Не знаю город «{city}»")

    lat, lon = coords

    days = 3
    if "недел" in when:
        days = 7
    elif "послезавтра" in when or "после завтра" in when:
        days = 3
    elif "завтра" in when:
        days = 2

    try:
        data = await _fetch_weather(lat, lon, days=days)
    except Exception as exc:
        return Response(speech=f"Не удалось получить прогноз: {exc}")

    daily = data.get("daily", {})
    times = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    codes = daily.get("weathercode", [])

    if not times:
        return Response(speech="Прогноз пуст")

    today = datetime.now().date()
    target = today
    if "завтра" in when:
        target += timedelta(days=1)
    elif "послезавтра" in when:
        target += timedelta(days=2)
    elif "недел" in when:
        pass

    lines = []
    for i, t_str in enumerate(times):
        day = datetime.fromisoformat(t_str).date()
        if "недел" not in when and day != target:
            continue
        desc = _decode_code(codes[i])
        lines.append(
            f"{day.strftime('%d.%m')}: {tmin[i]}…{tmax[i]}°C, {desc}"
        )
        if "недел" not in when:
            break

    if not lines:
        return Response(speech="Не нашёл прогноз на этот день")

    return Response(
        speech=f"{city.capitalize()}: " + "; ".join(lines),
        display=f"{city.capitalize()}: " + "\n".join(lines),
    )


def register(registry) -> Skill:
    skill = Skill("weather", description="Прогноз погоды через Open-Meteo")

    skill.command(
        "current",
        _current,
        patterns=[
            r"^погода\s+в\s+(?P<city>.+)$",
            r"^какая\s+погода\s+в\s+(?P<city>.+)$",
            r"^погода\s+(?P<city>.+)$",
        ],
        slots={
            "city": SlotSpec(name="city", type="string", required=True,
                             description="Название города"),
        },
        examples=["погода в Москве", "какая погода в Питере"],
        description="Текущая погода в городе",
    )

    skill.command(
        "forecast",
        _forecast,
        patterns=[
            r"^прогноз\s+(?:погоды\s+)?(?:на\s+)?(?P<when>завтра|послезавтра|на\s+неделю)\s+в\s+(?P<city>.+)$",
            r"^погода\s+(?:на\s+)?(?P<when>завтра|послезавтра|на\s+неделю)\s+в\s+(?P<city>.+)$",
            r"^какая\s+погода\s+(?:на\s+)?(?P<when>завтра|послезавтра|на\s+неделю)\s+в\s+(?P<city>.+)$",
        ],
        slots={
            "when": SlotSpec(name="when", type="string", required=True,
                             description="Когда: завтра, послезавтра, на неделю"),
            "city": SlotSpec(name="city", type="string", required=True,
                             description="Название города"),
        },
        examples=[
            "погода завтра в Москве",
            "прогноз на неделю в Новосибирске",
            "какая погода послезавтра в Питере",
        ],
        description="Прогноз погоды на завтра, послезавтра или неделю",
    )

    registry.add(skill)
    return skill