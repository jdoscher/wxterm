"""Text/number sources: NWS, Open-Meteo, NOAA tides, local sun/moon. Each fetch_* returns plain JSON-able data."""
from __future__ import annotations

import math
import re
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astral import LocationInfo, moon
from astral.sun import golden_hour, sun, SunDirection

from . import config
from .net import get_json

TZ = ZoneInfo(config.TZ)
NWS = "https://api.weather.gov"
_points: dict | None = None


def _c2f(v):
    return None if v is None else round(v * 9 / 5 + 32, 1)


def _val(o):
    return None if not o else o.get("value")


def points() -> dict:
    global _points
    if _points is None:
        _points = get_json(f"{NWS}/points/{config.LAT:.4f},{config.LON:.4f}")["properties"]
    return _points


# --- NWS -------------------------------------------------------------------

def fetch_obs() -> dict:
    p = get_json(f"{NWS}/stations/{config.OBS_STATION}/observations/latest")["properties"]
    kmh = 0.621371
    ws, wg = _val(p.get("windSpeed")), _val(p.get("windGust"))
    pa = _val(p.get("barometricPressure")) or _val(p.get("seaLevelPressure"))
    vis = _val(p.get("visibility"))
    return {
        "station": config.OBS_STATION,
        "time": p.get("timestamp"),
        "text": p.get("textDescription") or "",
        "temp_f": _c2f(_val(p.get("temperature"))),
        "dewpoint_f": _c2f(_val(p.get("dewpoint"))),
        "humidity": None if _val(p.get("relativeHumidity")) is None else round(_val(p.get("relativeHumidity"))),
        "wind_dir": _val(p.get("windDirection")),
        "wind_mph": None if ws is None else round(ws * kmh),
        "gust_mph": None if wg is None else round(wg * kmh),
        "pressure_inhg": None if pa is None else round(pa / 3386.389, 2),
        "visibility_mi": None if vis is None else round(vis / 1609.344, 1),
        "heat_index_f": _c2f(_val(p.get("heatIndex"))),
        "wind_chill_f": _c2f(_val(p.get("windChill"))),
    }


def fetch_forecast() -> dict:
    p = get_json(points()["forecast"])["properties"]
    out = []
    for per in p["periods"]:
        pop = (per.get("probabilityOfPrecipitation") or {}).get("value")
        out.append({k: per.get(k) for k in ("name", "startTime", "isDaytime", "temperature", "windSpeed",
                                            "windDirection", "shortForecast", "detailedForecast")} | {"pop": pop or 0})
    return {"updated": p.get("updateTime") or p.get("updated"), "periods": out}


def fetch_alerts() -> dict:
    d = get_json(f"{NWS}/alerts/active?point={config.LAT:.4f},{config.LON:.4f}", accept="application/geo+json")
    alerts = []
    for f in d.get("features", []):
        a = f["properties"]
        alerts.append({k: a.get(k) for k in ("event", "severity", "urgency", "headline", "onset", "ends",
                                             "expires", "description", "instruction")})
    return {"alerts": alerts}


def fetch_afd() -> dict:
    office = points()["gridId"]
    lst = get_json(f"{NWS}/products/types/AFD/locations/{office}")
    items = lst.get("@graph", [])
    if not items:
        return {"office": office, "text": "", "issued": None}
    prod = get_json(f"{NWS}/products/{items[0]['id']}")
    text = prod.get("productText", "")
    sections = {}
    for m in re.finditer(r"^\.([A-Z][A-Z /&]+?)\.\.\.(.*?)(?=^\.[A-Z][A-Z /&]+?\.\.\.|^&&|\Z)", text, re.S | re.M):
        body = re.sub(r"\s+", " ", m.group(2)).strip()
        sections[m.group(1).strip()] = body
    return {"office": office, "issued": prod.get("issuanceTime"), "sections": sections}


# --- Open-Meteo ------------------------------------------------------------

def _om(base: str, params: dict) -> dict:
    return get_json(f"{base}?{urllib.parse.urlencode(params)}", timeout=20)


def fetch_openmeteo() -> dict:
    d = _om("https://api.open-meteo.com/v1/forecast", {
        "latitude": config.LAT, "longitude": config.LON, "timezone": config.TZ,
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
        "past_days": 1, "forecast_days": 8,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,"
                   "wind_direction_10m,wind_gusts_10m,pressure_msl,cloud_cover,uv_index,is_day",
        "hourly": "temperature_2m,apparent_temperature,dew_point_2m,precipitation_probability,precipitation,"
                  "pressure_msl,cloud_cover,wind_speed_10m,uv_index",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,uv_index_max,wind_speed_10m_max",
    })
    return {k: d.get(k) for k in ("current", "hourly", "daily", "elevation")}


def fetch_marine() -> dict:
    if config.SURF_LAT is None:
        return {"current": {}, "hourly": {}, "unsupported": True}
    d = _om("https://marine-api.open-meteo.com/v1/marine", {
        "latitude": config.SURF_LAT, "longitude": config.SURF_LON, "timezone": config.TZ,
        "length_unit": "imperial", "temperature_unit": "fahrenheit", "forecast_days": 3, "past_days": 1,
        "current": "wave_height,wave_period,wave_direction,swell_wave_height,swell_wave_period,"
                   "swell_wave_direction,sea_surface_temperature",
        "hourly": "wave_height,swell_wave_height,wave_period",
    })
    return {k: d.get(k) for k in ("current", "hourly")}


def fetch_transect() -> dict:
    names, lats, lons = zip(*config.TRANSECT)
    d = _om("https://api.open-meteo.com/v1/forecast", {
        "latitude": ",".join(map(str, lats)), "longitude": ",".join(map(str, lons)), "timezone": config.TZ,
        "temperature_unit": "fahrenheit", "current": "temperature_2m,weather_code,relative_humidity_2m",
        "daily": "temperature_2m_max,temperature_2m_min", "forecast_days": 1,
    })
    d = d if isinstance(d, list) else [d]
    return {"points": [{"name": n, "elevation_ft": round(x["elevation"] * 3.28084),
                        "temp_f": x["current"]["temperature_2m"], "code": x["current"]["weather_code"],
                        "humidity": x["current"]["relative_humidity_2m"],
                        "hi": x["daily"]["temperature_2m_max"][0], "lo": x["daily"]["temperature_2m_min"][0]}
                       for n, x in zip(names, d)]}


# --- NOAA CO-OPS tides -----------------------------------------------------

COOPS = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


def _coops(**params) -> dict:
    base = {"station": config.TIDE_STATION, "units": "english", "time_zone": "lst_ldt", "format": "json",
            "application": "wxterm"}
    return get_json(f"{COOPS}?{urllib.parse.urlencode(base | params)}")


def fetch_tides() -> dict:
    begin = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y%m%d")
    curve = _coops(product="predictions", datum=config.TIDE_DATUM, interval="6", begin_date=begin, range="96")
    hilo = _coops(product="predictions", datum=config.TIDE_DATUM, interval="hilo", begin_date=begin, range="96")
    return {"station": config.TIDE_STATION, "datum": config.TIDE_DATUM,
            "curve": [[p["t"], float(p["v"])] for p in curve.get("predictions", [])],
            "hilo": [[p["t"], float(p["v"]), p["type"]] for p in hilo.get("predictions", [])]}


def fetch_water_temp() -> dict:
    rows = []
    if config.WATER_TEMP_STATION:
        d = _coops(product="water_temperature", date="latest", station=config.WATER_TEMP_STATION)
        rows = d.get("data") or []
    if not rows and config.SST_LAT is not None:
        from .world import fetch_sst
        return fetch_sst()
    if not rows:
        d = d if config.WATER_TEMP_STATION else {}
        return {"temp_f": None, "time": None, "error": (d.get("error") or {}).get("message")}
    return {"temp_f": float(rows[-1]["v"]), "time": rows[-1]["t"], "flags": rows[-1].get("f")}


# --- Sun and moon (computed, no API) ---------------------------------------

PHASES = [(1.0, "NEW MOON"), (6.4, "WAXING CRESCENT"), (8.4, "FIRST QUARTER"), (13.8, "WAXING GIBBOUS"),
          (15.8, "FULL MOON"), (21.1, "WANING GIBBOUS"), (23.1, "LAST QUARTER"), (28.0, "WANING CRESCENT")]


def _iso(t):
    return None if t is None else t.astimezone(TZ).isoformat(timespec="minutes")


def astro() -> dict:
    loc = LocationInfo("home", "", config.TZ, config.LAT, config.LON)
    today = datetime.now(TZ).date()
    out: dict = {}
    for key, day in (("today", today), ("tomorrow", today + timedelta(days=1))):
        try:
            s = sun(loc.observer, date=day, tzinfo=TZ)
            gh = golden_hour(loc.observer, date=day, direction=SunDirection.SETTING, tzinfo=TZ)
            out[key] = {k: _iso(s[k]) for k in ("dawn", "sunrise", "noon", "sunset", "dusk")} | \
                {"golden_start": _iso(gh[0]), "golden_end": _iso(gh[1])}
        except ValueError:
            out[key] = {}
    age = moon.phase(today)  # 0..27.99 days
    illum = (1 - math.cos(2 * math.pi * age / 29.530588)) / 2
    name = next(n for lim, n in PHASES if age < lim) if age < 28 else "NEW MOON"

    def _next(target):  # next date the phase age crosses target (0 = new, 14 = full)
        prev = moon.phase(today)
        for i in range(1, 32):
            cur = moon.phase(today + timedelta(days=i))
            if (prev < target <= cur) or (target == 0 and cur < prev):
                return (today + timedelta(days=i)).isoformat()
            prev = cur
        return None

    rise = sett = None
    try:
        rise = moon.moonrise(loc.observer, today, tzinfo=TZ)
    except Exception:  # noqa: BLE001 - no moonrise on some days
        pass
    try:
        sett = moon.moonset(loc.observer, today, tzinfo=TZ)
    except Exception:  # noqa: BLE001
        pass
    out["moon"] = {"age": round(age, 2), "illumination": round(illum, 3), "phase": name,
                   "waxing": age < 14.77, "rise": _iso(rise), "set": _iso(sett),
                   "next_full": _next(14.0), "next_new": _next(0)}
    return out
