"""Air traffic for the AIR map: ADS-B aircraft around home, and flight routes by callsign.

- aircraft: adsb.fi open data (free for personal, non-commercial use; at most 1 request a second), with adsb.lol as
  the fallback when adsb.fi fails or rate limits us
- routes: adsbdb (origin and destination for an airline callsign)

Nothing here runs on a schedule. The display polls /api/aircraft every few seconds while the AIR map is on screen,
so a collector only calls these services while someone is looking. Upstream calls are serialised and spaced (Gate),
and answers are cached briefly so the display and the MCP server share one fetch.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse

from . import config
from .net import get

FI = "https://opendata.adsb.fi/api/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{dist}"
LOL = "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{dist}"
ADSBDB = "https://api.adsbdb.com/v0/callsign/{}"
MAX_DIST = 250  # NM, the most either feed serves

KEEP = ("hex", "type", "flight", "r", "t", "desc", "ownOp", "category", "alt_baro", "alt_geom", "gs", "track",
        "true_heading", "mag_heading", "baro_rate", "geom_rate", "squawk", "emergency", "nav_altitude_mcp",
        "lat", "lon", "seen_pos", "seen", "mlat", "tisb")


class Gate:
    """Serialise calls to one upstream and keep at least `gap` seconds between them."""

    def __init__(self, gap: float):
        self.gap, self.last, self.lock = gap, 0.0, threading.Lock()

    def __enter__(self):
        self.lock.acquire()
        wait = self.last + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def __exit__(self, *_):
        self.last = time.monotonic()
        self.lock.release()


_FI_GATE, _LOL_GATE, _ROUTE_GATE = Gate(1.1), Gate(1.1), Gate(0.25)
_FETCH_LOCK = threading.Lock()
_ac_cache: dict[int, tuple[float, dict]] = {}   # dist -> (fetched at, answer)
_fi_backoff_until = 0.0
_routes: dict[str, tuple[float, dict | None]] = {}  # callsign -> (expires, route or None)


def _slim(rows) -> list[dict]:
    out = []
    for a in rows or []:
        if not isinstance(a.get("lat"), (int, float)) or not isinstance(a.get("lon"), (int, float)):
            continue
        cat = a.get("category")
        if isinstance(cat, str) and cat.startswith("C"):  # surface vehicles and obstacles are not flights
            continue
        o = {k: a[k] for k in KEEP if a.get(k) is not None}
        for k in ("mlat", "tisb"):
            if isinstance(o.get(k), list):
                o[k] = bool(o[k])
        if isinstance(o.get("flight"), str):
            o["flight"] = o["flight"].strip()
        out.append(o)
    return out


def _fetch(gate: Gate, url: str) -> dict:
    with gate:
        return json.loads(get(url, timeout=8, tries=1, accept="application/json"))


def aircraft(dist: int) -> dict:
    """Aircraft within `dist` NM of home. Cached for 1.5 s; adsb.fi first, adsb.lol if that fails."""
    global _fi_backoff_until
    dist = max(1, min(MAX_DIST, int(dist)))
    hit = _ac_cache.get(dist)
    if hit and time.time() - hit[0] < 1.5:
        return hit[1]
    errors = []
    with _FETCH_LOCK:  # one fetch at a time; whoever waited behind it gets the fresh answer from the cache
        hit = _ac_cache.get(dist)
        if hit and time.time() - hit[0] < 1.5:
            return hit[1]
        q = {"lat": config.LAT, "lon": config.LON, "dist": dist}
        for name, gate, url, keys in (("ADSB.FI", _FI_GATE, FI, ("aircraft", "ac")),
                                      ("ADSB.LOL", _LOL_GATE, LOL, ("ac", "aircraft"))):
            if name == "ADSB.FI" and time.time() < _fi_backoff_until:
                continue
            try:
                j = _fetch(gate, url.format(**q))
            except Exception as e:  # noqa: BLE001 - try the next feed
                code = getattr(e, "code", None)
                errors.append(f"{name} {code or type(e).__name__}")
                if name == "ADSB.FI":
                    _fi_backoff_until = time.time() + (15 if code in (None, 429) or (code or 0) >= 500 else 60)
                continue
            rows = next((j[k] for k in keys if isinstance(j.get(k), list)), [])
            out = {"source": name, "now": round(time.time(), 2), "dist": dist, "home": [config.LAT, config.LON],
                   "ac": _slim(rows)}
            _ac_cache.clear()
            _ac_cache[dist] = (time.time(), out)
            return out
    raise RuntimeError("; ".join(errors) or "no aircraft feed")


def _airport(a: dict | None) -> dict | None:
    if not a:
        return None
    return {"iata": a.get("iata_code") or "", "icao": a.get("icao_code") or "", "name": a.get("name") or "",
            "city": a.get("municipality") or "", "country": a.get("country_iso_name") or "",
            "lat": a.get("latitude"), "lon": a.get("longitude")}


def route(cs: str) -> dict | None:
    """Origin and destination for an airline callsign (UAL123), or None. Known routes are kept for 6 hours."""
    hit = _routes.get(cs)
    if hit and hit[0] > time.time():
        return hit[1]
    ttl, val = 600, None
    try:
        with _ROUTE_GATE:
            body = get(ADSBDB.format(urllib.parse.quote(cs)), timeout=6, tries=1, accept="application/json")
        fr = (json.loads(body).get("response") or {})
        fr = fr.get("flightroute") if isinstance(fr, dict) else None
        if fr:
            val = {"airline": (fr.get("airline") or {}).get("name") or "", "iata": fr.get("callsign_iata") or "",
                   "origin": _airport(fr.get("origin")), "dest": _airport(fr.get("destination"))}
            ttl = 6 * 3600
        else:
            ttl = 3 * 3600
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) == 404:  # adsbdb answers 404 for callsigns it doesn't know
            ttl = 3 * 3600
    if len(_routes) > 5000:  # request threads share this dict, so iterate over a copy
        now = time.time()
        items = list(_routes.items())
        for k in [k for k, (exp, _) in items if exp < now] or [k for k, _ in items[:1000]]:
            _routes.pop(k, None)
    _routes[cs] = (time.time() + ttl, val)
    return val


def routes(callsigns: list[str]) -> dict[str, dict | None]:
    keep = [c for c in dict.fromkeys(s.strip().upper() for s in callsigns) if c.isalnum() and 2 <= len(c) <= 8]
    return {cs: route(cs) for cs in keep[:8]}
