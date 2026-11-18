"""Read the wxterm collectors and turn their JSON into compact, LLM-friendly summaries.

Every location's collector is reachable on the compose network as http://<name>:8091 (WXMCP_COLLECTOR overrides the
pattern). Location names come from /locations/order, the same file the display and gen-compose.py use.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOC_DIR = Path(os.environ.get("WXMCP_LOCATIONS", "/locations"))
STATE_DIR = Path(os.environ.get("WXMCP_STATE", "/state"))
COLLECTOR = os.environ.get("WXMCP_COLLECTOR", "http://{name}:8091")
CACHE_S = 20

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
       82: "heavy showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
       96: "thunderstorms with hail", 99: "thunderstorms with hail"}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


class WxError(Exception):
    """A problem worth telling the caller about (unknown location, collector down)."""


# --- locations ------------------------------------------------------------------------------------------------

def _env(name: str) -> dict:
    out = {}
    try:
        for line in (LOC_DIR / f"{name}.env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def locations() -> list[dict]:
    keep = {n for n in re.split(r"[\s,]+", os.environ.get("WXTERM_ONLY", "")) if n}  # kiosk.conf LOCATIONS
    out = []
    try:
        lines = (LOC_DIR / "order").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split("#", 1)[0].split()
        if len(f) == 2 and (LOC_DIR / f"{f[0]}.env").exists() and (not keep or f[0] in keep):
            env = _env(f[0])
            out.append({"name": f[0], "title": env.get("WX_NAME", f[0].upper()),
                        "provider": env.get("WX_PROVIDER", "nws")})
    return out


def display_state() -> dict:
    out = {}
    for fname in ("state.json", "display-status.json"):
        try:
            out.update(json.loads((STATE_DIR / fname).read_text()))
        except (OSError, ValueError):
            pass
    return out


def resolve(location: str | None) -> str:
    """A valid location name: the one asked for, else the one on screen, else the first."""
    names = [loc["name"] for loc in locations()]
    if not names:
        raise WxError("no locations configured")
    if location:
        key = location.strip().lower()
        for loc in locations():
            if key in (loc["name"], loc["title"].lower()):
                return loc["name"]
        raise WxError(f"unknown location {location!r}; choose one of: {', '.join(names)}")
    shown = display_state().get("location")
    return shown if shown in names else names[0]


# --- collector access -----------------------------------------------------------------------------------------

def _get(name: str, path: str, timeout: float = 10, cache: bool = True) -> Any:
    key = f"{name}{path}"
    with _lock:
        hit = _cache.get(key)
        if cache and hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    url = COLLECTOR.format(name=name) + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001
        raise WxError(f"collector for {name} is not answering ({type(e).__name__})") from e
    with _lock:
        _cache[key] = (time.time(), data)
    return data


def everything(name: str) -> dict:
    return _get(name, "/api/all")


def healthz(name: str) -> bool:
    try:
        return bool(_get(name, "/healthz", timeout=3, cache=False).get("ok"))
    except WxError:
        return False


def _src(d: dict, key: str):
    return ((d.get("sources") or {}).get(key) or {}).get("data")


def _tz(d: dict) -> ZoneInfo:
    try:
        return ZoneInfo((d.get("home") or {}).get("tz") or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _local(ts: str | None, tz: ZoneInfo, fmt: str = "%a %-d %b %-I:%M %p") -> str | None:
    """Format a timestamp (ISO with or without offset, or 'YYYY-MM-DD HH:MM' in local time) for people."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace(" ", "T"))
    except ValueError:
        return ts
    dt = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
    return dt.strftime(fmt)


def _compass(deg) -> str | None:
    return None if deg is None else COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]


def _r(v, n=0):
    return None if v is None else round(float(v), n) if n else round(float(v))


def _header(name: str, d: dict) -> dict:
    lab = d.get("labels") or {}
    return {"location": name, "title": lab.get("name"), "local_time": _local(d.get("now"), _tz(d))}


# --- summaries ------------------------------------------------------------------------------------------------

def current(name: str) -> dict:
    d = everything(name)
    tz = _tz(d)
    obs, om = _src(d, "obs") or {}, (_src(d, "openmeteo") or {})
    cur, daily = om.get("current") or {}, om.get("daily") or {}
    today = (datetime.now(tz).date().isoformat())
    i = (daily.get("time") or []).index(today) if today in (daily.get("time") or []) else None
    hi = (daily.get("temperature_2m_max") or [None])[i] if i is not None else None
    lo = (daily.get("temperature_2m_min") or [None])[i] if i is not None else None
    alerts = (_src(d, "alerts") or {}).get("alerts") or []
    astro = d.get("astro") or {}
    sun = astro.get("today") or {}
    return _header(name, d) | {
        "station": obs.get("station") or (d.get("labels") or {}).get("obs_station"),
        "observed": _local(obs.get("time"), tz),
        "conditions": obs.get("text") or WMO.get(cur.get("weather_code")),
        "temp_f": _r(obs.get("temp_f") if obs.get("temp_f") is not None else cur.get("temperature_2m")),
        "feels_like_f": _r(obs.get("heat_index_f") or obs.get("wind_chill_f") or cur.get("apparent_temperature")),
        "today_high_f": _r(hi), "today_low_f": _r(lo),
        "humidity_pct": _r(obs.get("humidity") if obs.get("humidity") is not None
                           else cur.get("relative_humidity_2m")),
        "dew_point_f": _r(obs.get("dewpoint_f")),
        "wind": {"from": obs.get("wind_dir") or _compass(cur.get("wind_direction_10m")),
                 "mph": _r(obs.get("wind_mph") if obs.get("wind_mph") is not None else cur.get("wind_speed_10m")),
                 "gust_mph": _r(obs.get("gust_mph") or cur.get("wind_gusts_10m"))},
        "pressure_inhg": _r(obs.get("pressure_inhg"), 2) or (_r(cur.get("pressure_msl") * 0.02953, 2)
                                                             if cur.get("pressure_msl") else None),
        "visibility_mi": _r(obs.get("visibility_mi")),
        "cloud_cover_pct": _r(cur.get("cloud_cover")), "uv_index": _r(cur.get("uv_index"), 1),
        "sunrise": _local(sun.get("sunrise"), tz, "%-I:%M %p"), "sunset": _local(sun.get("sunset"), tz, "%-I:%M %p"),
        "alerts": [a.get("event") for a in alerts],
    }


def forecast(name: str, hours: int = 12) -> dict:
    d = everything(name)
    tz = _tz(d)
    out = _header(name, d)
    periods = (_src(d, "forecast") or {}).get("periods") or []
    if periods:
        out["source"] = (d.get("labels") or {}).get("forecast_src") or "NWS"
        out["periods"] = [{"name": p.get("name"), "temp_f": p.get("temperature"), "rain_pct": p.get("pop"),
                           "wind": f"{p.get('windDirection', '')} {p.get('windSpeed', '')}".strip(),
                           "forecast": p.get("detailedForecast") or p.get("shortForecast")} for p in periods[:10]]
    om = _src(d, "openmeteo") or {}
    daily = om.get("daily") or {}
    if daily.get("time"):
        today = datetime.now(tz).date().isoformat()
        out["daily"] = [{"date": t, "conditions": WMO.get(daily["weather_code"][i]),
                         "high_f": _r(daily["temperature_2m_max"][i]), "low_f": _r(daily["temperature_2m_min"][i]),
                         "rain_pct": daily.get("precipitation_probability_max", [None] * 99)[i],
                         "rain_in": _r(daily.get("precipitation_sum", [None] * 99)[i], 2)}
                        for i, t in enumerate(daily["time"]) if t >= today][:7]
    hourly = om.get("hourly") or {}
    hours = max(0, min(int(hours), 48))
    if hours and hourly.get("time"):
        now = datetime.now(tz).replace(minute=0, second=0, microsecond=0, tzinfo=None)
        rows = []
        for i, t in enumerate(hourly["time"]):
            if datetime.fromisoformat(t) < now:
                continue
            rows.append({"time": datetime.fromisoformat(t).strftime("%a %-I %p"),
                         "temp_f": _r(hourly["temperature_2m"][i]),
                         "rain_pct": hourly.get("precipitation_probability", [None] * 999)[i],
                         "wind_mph": _r(hourly.get("wind_speed_10m", [None] * 999)[i])})
            if len(rows) >= hours:
                break
        out["hourly"] = rows
    return out


def alerts(name: str) -> dict:
    d = everything(name)
    tz = _tz(d)
    src = d.get("sources", {}).get("alerts") or {}
    if src.get("error") == "unsupported" or (d.get("labels") or {}).get("alerts_src") is None:
        return _header(name, d) | {"supported": False, "note": "weather alerts are only available for US locations"}
    items = (src.get("data") or {}).get("alerts") or []
    return _header(name, d) | {"supported": True, "count": len(items), "alerts": [
        {"event": a.get("event"), "severity": a.get("severity"), "headline": a.get("headline"),
         "ends": _local(a.get("ends") or a.get("expires"), tz),
         "text": (a.get("description") or "")[:2500], "instructions": (a.get("instruction") or "")[:800]}
        for a in items]}


def discussion(name: str, section: str | None = None) -> dict:
    d = everything(name)
    afd = _src(d, "afd")
    if not afd:
        return _header(name, d) | {"supported": False,
                                   "note": "forecast discussions are written by NWS offices (US locations only)"}
    secs = afd.get("sections") or {}
    out = _header(name, d) | {"office": afd.get("office"), "issued": _local(afd.get("issued"), _tz(d)),
                              "sections": list(secs)}
    if section:
        key = next((k for k in secs if section.strip().lower() in k.lower()), None)
        if not key:
            raise WxError(f"no section like {section!r}; sections: {', '.join(secs)}")
        out["section"], out["text"] = key, secs[key][:6000]
    else:  # the summary-ish sections first, capped so a long discussion doesn't flood the context
        want = ("KEY", "SYNOPSIS", "SHORT", "DISCUSSION")
        keep = [k for k in secs if any(w in k.upper() for w in want)] or list(secs)
        out["text"] = "\n\n".join(f"{k}\n{secs[k]}" for k in keep)[:6000]
    return out


def _tide_now(curve: list, tz: ZoneInfo) -> tuple[float | None, str | None]:
    now = datetime.now(tz).replace(tzinfo=None)
    pts = [(datetime.fromisoformat(t.replace(" ", "T")), v) for t, v in curve]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:], strict=False):
        if t0 <= now <= t1:
            f = (now - t0) / (t1 - t0) if t1 > t0 else 0
            return round(v0 + (v1 - v0) * f, 1), "rising" if v1 > v0 else "falling"
    return None, None


def tides_moon_sun(name: str) -> dict:
    d = everything(name)
    tz = _tz(d)
    lab = d.get("labels") or {}
    out = _header(name, d)
    t = _src(d, "tides") or {}
    if t.get("hilo") or t.get("curve"):
        level, trend = _tide_now(t.get("curve") or [], tz)
        now = datetime.now(tz).replace(tzinfo=None)
        nxt = [(h, v, k) for h, v, k in t.get("hilo") or []
               if now <= datetime.fromisoformat(h.replace(" ", "T")) <= now + timedelta(hours=36)]
        out["tide"] = {"station": lab.get("tide_name"), "source": "NOAA" if lab.get("tide_source") != "model"
                       else "Open-Meteo sea-level model", "datum": t.get("datum", "MLLW"), "now_ft": level,
                       "trend": trend, "next": [{"type": "high" if k == "H" else "low", "time": _local(h, tz),
                                                 "ft": round(v, 1)} for h, v, k in nxt]}
    else:
        out["tide"] = None
    wt = _src(d, "water_temp") or {}
    marine = (_src(d, "marine") or {}).get("current") or {}
    out["water_temp_f"] = _r(wt.get("temp_f") if wt.get("temp_f") is not None
                             else marine.get("sea_surface_temperature"))
    astro = d.get("astro") or {}
    moon, sun = astro.get("moon") or {}, astro.get("today") or {}
    out["moon"] = {"phase": (moon.get("phase") or "").lower() or None,
                   "illumination_pct": _r((moon.get("illumination") or 0) * 100), "age_days": _r(moon.get("age"), 1),
                   "rise": _local(moon.get("rise"), tz, "%-I:%M %p"), "set": _local(moon.get("set"), tz, "%-I:%M %p"),
                   "next_full": moon.get("next_full"), "next_new": moon.get("next_new")}
    hm = "%-I:%M %p"
    out["sun"] = {k: _local(sun.get(k), tz, hm) for k in ("dawn", "sunrise", "noon", "sunset", "dusk")} | {
        "golden_hour": f"{_local(sun.get('golden_start'), tz, hm)} to {_local(sun.get('golden_end'), tz, hm)}"}
    return out


def marine(name: str) -> dict:
    d = everything(name)
    lab = d.get("labels") or {}
    m = (_src(d, "marine") or {}).get("current") or {}
    if not m:
        return _header(name, d) | {"supported": False, "note": "no open water with wave data near this location"}
    return _header(name, d) | {  # the collector asks Open-Meteo for imperial units: heights are already feet
        "spot": lab.get("surf_name"),
        "waves_ft": _r(m.get("wave_height"), 1),
        "wave_period_s": _r(m.get("wave_period")), "wave_from": _compass(m.get("wave_direction")),
        "swell_ft": _r(m.get("swell_wave_height"), 1),
        "swell_period_s": _r(m.get("swell_wave_period")), "swell_from": _compass(m.get("swell_wave_direction")),
        "sea_surface_temp_f": _r(m.get("sea_surface_temperature")),
    }


def transect(name: str) -> dict:
    d = everything(name)
    lab = d.get("labels") or {}
    pts = (_src(d, "transect") or {}).get("points") or []
    return _header(name, d) | {"title": (lab.get("transect_title") or "").lower(), "points": [
        {"place": p.get("name"), "elevation_ft": p.get("elevation_ft"), "temp_f": _r(p.get("temp_f")),
         "conditions": WMO.get(p.get("code")), "high_f": _r(p.get("hi")), "low_f": _r(p.get("lo"))} for p in pts]}


# --- radar ----------------------------------------------------------------------------------------------------

def _km(lat0, lon0, lat1, lon1) -> float:
    p = math.pi / 180
    h = (math.sin((lat1 - lat0) * p / 2) ** 2 +
         math.cos(lat0 * p) * math.cos(lat1 * p) * math.sin((lon1 - lon0) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


def _bearing(lat0, lon0, lat1, lon1) -> str:
    y = math.sin(math.radians(lon1 - lon0)) * math.cos(math.radians(lat1))
    x = (math.cos(math.radians(lat0)) * math.sin(math.radians(lat1)) -
         math.sin(math.radians(lat0)) * math.cos(math.radians(lat1)) * math.cos(math.radians(lon1 - lon0)))
    return _compass(math.degrees(math.atan2(y, x)))


def _cells(rows: list[str], bbox) -> list[tuple[float, float, int, bool]]:
    lon0, lon1, lat0, lat1 = bbox
    h, w = len(rows), len(rows[0]) if rows else 0
    out = []
    for j, row in enumerate(rows):
        lat = lat1 - (lat1 - lat0) * (j + 0.5) / h
        for i, ch in enumerate(row):
            if ch == "0":
                continue
            snow = ch.isalpha()
            level = ord(ch) - ord("a") if snow else int(ch)  # b-f = snow levels 1-5
            out.append((lat, lon0 + (lon1 - lon0) * (i + 0.5) / w, level, snow))
    return out


def radar(name: str) -> dict:
    d = everything(name)
    home = d.get("home") or {}
    lat, lon = home.get("lat"), home.get("lon")
    r = _get(name, "/api/radar?w=120&h=100", timeout=30)
    frames = r.get("frames") or []
    tz = _tz(d)
    out = _header(name, d) | {"source": r.get("source"), "frames": len(frames)}
    if not frames or lat is None:
        return out | {"summary": "no radar frames yet"}
    bbox = r["bbox"]
    out["map_covers_km"] = [round(_km(lat, bbox[0], lat, bbox[1])), round(_km(bbox[2], lon, bbox[3], lon))]
    out["latest_frame"] = _local(datetime.fromtimestamp(frames[-1]["t"]).astimezone().isoformat(), tz)
    first, last = _cells(frames[0]["rows"], bbox), _cells(frames[-1]["rows"], bbox)
    total = r["w"] * r["h"]
    out["coverage_pct"] = round(100 * len(last) / total, 1)
    out["coverage_pct_start_of_loop"] = round(100 * len(first) / total, 1)
    if not last:
        return out | {"summary": "no precipitation on the map"}
    near = min(last, key=lambda c: _km(lat, lon, c[0], c[1]))
    dist = _km(lat, lon, near[0], near[1])
    top = max(c[2] for c in last)
    snow = sum(c[3] for c in last) / len(last)
    words = ["none", "very light", "light", "moderate", "heavy", "very heavy"]
    here = max((c[2] for c in last if _km(lat, lon, c[0], c[1]) <= 8), default=0)
    out["at_location"] = words[min(here, 5)]
    out["strongest_on_map_dbz"] = top * 10
    out["intensity"] = words[min(top, 5)]
    out["type"] = "snow" if snow > 0.6 else "mixed rain and snow" if snow > 0.1 else "rain"
    out["nearest_km"], out["nearest_mi"] = round(dist), round(dist / 1.609)
    out["nearest_direction"] = _bearing(lat, lon, near[0], near[1]) if dist > 2 else "overhead"
    if first:  # centroid drift over the loop, a rough movement estimate
        c0 = (sum(c[0] for c in first) / len(first), sum(c[1] for c in first) / len(first))
        c1 = (sum(c[0] for c in last) / len(last), sum(c[1] for c in last) / len(last))
        hrs = max((frames[-1]["t"] - frames[0]["t"]) / 3600, 0.1)
        moved = _km(c0[0], c0[1], c1[0], c1[1])
        if moved / hrs > 5 and abs(len(last) - len(first)) < 0.5 * max(len(first), 1):
            out["moving_toward"] = _bearing(c0[0], c0[1], c1[0], c1[1])
            out["speed_mph"] = round(moved / hrs / 1.609)
    trend = len(last) - len(first)
    out["trend"] = "growing" if trend > 0.2 * max(len(first), 1) else (
        "shrinking" if -trend > 0.2 * max(len(first), 1) else "steady")
    where = f"{words[min(here, 5)]} {out['type']} over the location" if here else \
        f"nearest {out['type']} {out['nearest_mi']} mi {out['nearest_direction']}"
    out["summary"] = f"{where}; heaviest on the map {out['intensity']}; {out['trend']}" + (
        f", moving {out['moving_toward']} ~{out['speed_mph']} mph" if "moving_toward" in out else "")
    return out


def hazards(name: str, radius_mi: float = 300) -> dict:
    """Earthquakes (past week) and wildfires within radius_mi of the location, plus the day's largest quake
    anywhere. The worldwide lists come from the first location's collector, as on the display."""
    d = everything(name)
    tz = _tz(d)
    home = d.get("home") or {}
    lat, lon = home.get("lat"), home.get("lon")
    radius_mi = max(10.0, min(float(radius_mi), 1000.0))
    first = (locations() or [{"name": name}])[0]["name"]
    try:
        qd = _get(first, "/api/quakes", timeout=60)
    except WxError:
        qd = _get(name, "/api/quakes", timeout=60)
    try:
        fd = _get(first, "/api/fires", timeout=60)
    except WxError:
        fd = _get(name, "/api/fires", timeout=60)
    now = time.time()

    def near(rows):
        out = []
        for r in rows or []:
            km = _km(lat, lon, r["lat"], r["lon"])
            if km / 1.609 <= radius_mi:
                out.append((km, r))
        return out

    quakes = sorted(near(q for q in qd.get("quakes") or [] if q["mag"] >= (1.5 if now - q["t"] < 86400 else 2.5)),
                    key=lambda v: -v[1]["t"])
    today = [q for q in qd.get("quakes") or [] if now - q["t"] < 86400]
    big = max(today, key=lambda q: q["mag"]) if today else None
    fires = sorted(near(fd.get("fires") or []), key=lambda v: v[0])

    def when(t):
        return _local(datetime.fromtimestamp(t).astimezone().isoformat(), tz) if t else None

    return _header(name, d) | {
        "radius_mi": round(radius_mi),
        "earthquakes": {
            "sources": qd.get("sources"), "error": qd.get("error"), "count_past_week": len(quakes),
            "largest_worldwide_today": {"mag": big["mag"], "place": big["place"], "time": when(big["t"])}
            if big else None,
            "near": [{"mag": q["mag"], "time": when(q["t"]), "hours_ago": round((now - q["t"]) / 3600, 1),
                      "place": q["place"],
                      "depth_km": round(q["depth_km"], 1) if q.get("depth_km") is not None else None,
                      "distance_mi": round(km / 1.609),
                      "direction": _bearing(lat, lon, q["lat"], q["lon"]), "felt_reports": q.get("felt"),
                      "tsunami": q.get("tsunami") or None, "source": q["src"]} for km, q in quakes[:25]],
        },
        "wildfires": {
            "sources": fd.get("sources"), "error": fd.get("error"), "count": len(fires),
            "note": "US fires with size and containment from NIFC; large fires elsewhere from GDACS",
            "near": [{"name": f["name"].title() if f["src"] == "NIFC" else f"Wildfire ({f.get('country')})",
                      "acres": f.get("acres"), "hectares": f.get("ha"), "percent_contained": f.get("contained"),
                      "discovered": when(f.get("discovered")), "distance_mi": round(km / 1.609),
                      "direction": _bearing(lat, lon, f["lat"], f["lon"]), "source": f["src"]}
                     for km, f in fires[:25]],
        },
    }


def health(name: str) -> dict:
    """Per-source freshness: ok, stale (older than 2x its interval), or error."""
    d = everything(name)
    now = time.time()
    out = {}
    srcs = dict(d.get("sources") or {})
    if d.get("radar_meta"):
        srcs["radar"] = d["radar_meta"]
    for k, v in srcs.items():
        if v.get("error") == "unsupported":
            continue
        age = None
        if v.get("ok_at"):
            age = now - datetime.fromisoformat(v["ok_at"]).timestamp()
        state = "error" if v.get("error") and age is None else (
            "stale" if age is not None and age > 2 * (v.get("interval") or 3600) + 120 else "ok")
        out[k] = {"state": state, "age_min": None if age is None else round(age / 60),
                  "error": (v.get("error") or None) and str(v["error"])[:120]}
    return out
