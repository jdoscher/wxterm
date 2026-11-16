"""Sources for places outside NWS coverage (WX_PROVIDER=global), plus model tides for any coast.

- observations: airport METAR from aviationweather.gov (free, no key)
- forecast: Open-Meteo daily, shaped like NWS forecast periods so the display treats both alike
- tides: Open-Meteo marine sea_level_height_msl (a tide+surge model), highs/lows found from the curve
Alerts and the forecast discussion are NWS products with no free global equivalent; they report unsupported.
"""
from __future__ import annotations

import math
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config
from .net import get_json

WMO = {0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing Fog",
       51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle", 56: "Freezing Drizzle", 57: "Freezing Drizzle",
       61: "Light Rain", 63: "Rain", 65: "Heavy Rain", 66: "Freezing Rain", 67: "Freezing Rain",
       71: "Light Snow", 73: "Snow", 75: "Heavy Snow", 77: "Snow Grains", 80: "Rain Showers", 81: "Rain Showers",
       82: "Heavy Rain Showers", 85: "Snow Showers", 86: "Heavy Snow Showers", 95: "Thunderstorms",
       96: "Thunderstorms With Hail", 99: "Thunderstorms With Hail"}
DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
COVER = {"CLR": "Clear", "SKC": "Clear", "CAVOK": "Clear", "FEW": "Mostly Clear", "SCT": "Partly Cloudy",
         "BKN": "Mostly Cloudy", "OVC": "Overcast", "OVX": "Obscured"}
WX = [("TS", "Thunderstorm"), ("FZRA", "Freezing Rain"), ("SHRA", "Rain Showers"), ("SHSN", "Snow Showers"),
      ("RA", "Rain"), ("SN", "Snow"), ("DZ", "Drizzle"), ("GR", "Hail"), ("FG", "Fog"), ("BR", "Mist"),
      ("HZ", "Haze"), ("FU", "Smoke")]
OM = "https://api.open-meteo.com/v1/forecast"
MARINE = "https://marine-api.open-meteo.com/v1/marine"


def _compass(deg) -> str:
    return "" if deg is None else DIRS[int((deg % 360) / 22.5 + 0.5) % 16]


def _c2f(c):
    return None if c is None else round(c * 9 / 5 + 32, 1)


def _metar_text(m: dict) -> str:
    wx = m.get("wxString") or ""
    for code, name in WX:
        if code in wx:
            return ("Light " if wx.startswith("-") else "Heavy " if wx.startswith("+") else "") + name
    return COVER.get(m.get("cover") or "", "")


def fetch_obs() -> dict:
    rows = get_json(f"https://aviationweather.gov/api/data/metar?ids={config.OBS_STATION}&format=json")
    if not rows:
        raise RuntimeError(f"no METAR for {config.OBS_STATION}")
    m = rows[0]
    t, d = m.get("temp"), m.get("dewp")
    rh = None
    if t is not None and d is not None:  # Magnus approximation
        a, b = 17.625, 243.04
        rh = round(100 * math.exp(a * d / (b + d)) / math.exp(a * t / (b + t)))
    vis = m.get("visib")
    vis_mi = 10.0 if vis in ("6+", "10+") else (float(vis) if isinstance(vis, (int, float)) else None)
    wdir = m.get("wdir") if isinstance(m.get("wdir"), (int, float)) else None
    kt = 1.15078
    return {
        "station": config.OBS_STATION,
        "time": m.get("reportTime"),
        "text": _metar_text(m),
        "temp_f": _c2f(t), "dewpoint_f": _c2f(d), "humidity": rh,
        "wind_dir": wdir,
        "wind_mph": None if m.get("wspd") is None else round(m["wspd"] * kt),
        "gust_mph": None if not m.get("wgst") else round(m["wgst"] * kt),
        "pressure_inhg": None if not m.get("altim") else round(m["altim"] / 33.8639, 2),
        "visibility_mi": vis_mi,
        "raw": m.get("rawOb"),
    }


def fetch_forecast() -> dict:
    q = {"latitude": config.LAT, "longitude": config.LON, "timezone": config.TZ, "forecast_days": 7,
         "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
         "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                  "wind_speed_10m_max,wind_direction_10m_dominant"}
    d = get_json(f"{OM}?{urllib.parse.urlencode(q)}", timeout=20)["daily"]
    periods = []
    for i, day in enumerate(d["time"]):
        text = WMO.get(d["weather_code"][i], "")
        common = {"shortForecast": text, "pop": d["precipitation_probability_max"][i] or 0,
                  "windSpeed": f"{round(d['wind_speed_10m_max'][i] or 0)} mph",
                  "windDirection": _compass(d["wind_direction_10m_dominant"][i])}
        periods.append({"name": day, "startTime": f"{day}T06:00", "isDaytime": True,
                        "temperature": round(d["temperature_2m_max"][i])} | common)
        periods.append({"name": day, "startTime": f"{day}T18:00", "isDaytime": False,
                        "temperature": round(d["temperature_2m_min"][i])} | common)
    now = datetime.now(ZoneInfo(config.TZ)).strftime("%Y-%m-%dT%H:%M")
    periods = [p for p in periods if not (p["isDaytime"] and p["startTime"][:10] == now[:10] and now[11:] > "17:00")]
    return {"updated": datetime.now(ZoneInfo(config.TZ)).isoformat(timespec="minutes"), "periods": periods,
            "source": "OPEN-METEO"}


def fetch_alerts() -> dict:
    return {"alerts": [], "unsupported": True}


def fetch_afd() -> dict:
    return {"office": None, "issued": None, "sections": {}, "unsupported": True}


def fetch_model_tides() -> dict:
    """Tide curve from the Open-Meteo marine sea-level model at (TIDE_LAT, TIDE_LON), feet above MSL."""
    q = {"latitude": config.TIDE_LAT, "longitude": config.TIDE_LON, "timezone": config.TZ,
         "hourly": "sea_level_height_msl", "past_days": 1, "forecast_days": 3, "current": "sea_surface_temperature",
         "temperature_unit": "fahrenheit"}
    d = get_json(f"{MARINE}?{urllib.parse.urlencode(q)}", timeout=20)
    times, vals = d["hourly"]["time"], d["hourly"]["sea_level_height_msl"]
    pts = [(t, v * 3.28084) for t, v in zip(times, vals) if v is not None]
    if len(pts) < 6:
        raise RuntimeError("no sea level model data at the tide point")
    hilo = []
    for i in range(1, len(pts) - 1):
        (_, a), (t, b), (_, c) = pts[i - 1], pts[i], pts[i + 1]
        if (b > a and b >= c) or (b < a and b <= c):
            den = a - 2 * b + c  # parabola through the three hourly points refines time and height
            off = 0.5 * (a - c) / den if den else 0.0
            off = max(-0.5, min(0.5, off))
            peak = b - 0.25 * (a - c) * off
            when = datetime.fromisoformat(t) + timedelta(hours=float(off))
            hilo.append([when.strftime("%Y-%m-%d %H:%M"), round(peak, 2), "H" if b > a else "L"])
    hilo = _significant(hilo, 0.25)
    return {"station": "MODEL", "datum": "MSL", "curve": [[t, round(v, 3)] for t, v in pts], "hilo": hilo,
            "sst_f": (d.get("current") or {}).get("sea_surface_temperature")}


def _significant(ext: list, min_range_ft: float) -> list:
    """Drop high/low pairs smaller than min_range_ft (surge noise on small-tide coasts like the Oslofjord)."""
    out: list = []
    for e in ext:
        if out and out[-1][2] == e[2]:  # two highs (or lows) in a row: keep the more extreme
            if (e[1] > out[-1][1]) == (e[2] == "H"):
                out[-1] = e
            continue
        if out and abs(e[1] - out[-1][1]) < min_range_ft:
            out.pop()  # this wiggle is noise: remove both ends
            if out and out[-1][2] == e[2]:
                if (e[1] > out[-1][1]) == (e[2] == "H"):
                    out[-1] = e
                continue
            if not out:
                out.append(e)
            continue
        out.append(e)
    return out


def fetch_sst() -> dict:
    """Water temperature from the marine model when there's no NOAA sensor."""
    q = {"latitude": config.SST_LAT, "longitude": config.SST_LON, "current": "sea_surface_temperature",
         "temperature_unit": "fahrenheit"}
    d = get_json(f"{MARINE}?{urllib.parse.urlencode(q)}", timeout=20)
    cur = d.get("current") or {}
    return {"temp_f": cur.get("sea_surface_temperature"), "time": cur.get("time"), "model": True}
