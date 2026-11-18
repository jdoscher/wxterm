"""Earthquakes and wildfires for the map's hazard layers. Every source is free and needs no key.

Earthquakes: USGS (worldwide, every size in the US, the past day; M2.5+ for the past week) merged with EMSC (the
European-Mediterranean centre, worldwide M2+ for the past 3 days, quicker for quakes outside the US). An EMSC event
within a minute and 100 km of a USGS one is the same quake and is dropped.

Wildfires: NIFC's WFIGS service (US incidents and their mapped perimeters, wildfires only, no prescribed burns) and
GDACS (the UN/EU global disaster alert system: large wildfires worldwide, outside the US).

Both are fetched on demand, at most every QUAKE_TTL / FIRE_TTL seconds, kept on disk, and the last good copy is served
when a source fails. The display asks one collector (the first location) for the worldwide lists and draws what's on
its own map, so a house of ten collectors still makes one request per source.

GET /api/quakes -> {"fetched", "error", "sources", "quakes": [{id, t, lat, lon, depth_km, mag, mag_type, place, src,
                    felt, tsunami, alert}]}
GET /api/fires  -> {"fetched", "error", "sources", "fires": [{id, name, lat, lon, acres, ha, contained, discovered,
                    updated, src, country}], "perimeters": [{name, acres, contained, rings: [[[lon, lat], ...]]}]}
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from . import config
from .net import get

log = logging.getLogger("wxcollect.hazards")

QUAKE_TTL = 300          # earthquakes: fetch again after 5 minutes
FIRE_TTL = 900           # wildfires: 15 minutes (NIFC and GDACS update a few times an hour at most)
USGS = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{}.geojson"
EMSC = "https://www.seismicportal.eu/fdsnws/event/1/query"
NIFC = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services"
GDACS = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
CONTAINED_KEEP_DAYS = 3  # fully contained fires stay on the map this long after their last update
ACRES_PER_HA = 2.47105

_locks = {"quakes": threading.Lock(), "fires": threading.Lock()}
_mem: dict[str, dict] = {}


# --- cache --------------------------------------------------------------------------------------------------------

def _path(kind: str) -> Path:
    return Path(config.DATA_DIR) / f"hazards-{kind}.json"


def _load(kind: str) -> dict | None:
    if kind in _mem:
        return _mem[kind]
    try:
        _mem[kind] = json.loads(_path(kind).read_text())
        return _mem[kind]
    except (OSError, ValueError):
        return None


def _save(kind: str, ent: dict):
    _mem[kind] = ent
    try:
        p = _path(kind)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(ent, separators=(",", ":")))
        tmp.replace(p)
    except OSError as e:
        log.warning("%s cache save failed: %s", kind, e)


def _cached(kind: str, ttl: float, fetch, now: float | None) -> dict:
    """The cached list, refreshed first if it's older than ttl. A failed source keeps its part of the old copy."""
    now = now or time.time()
    with _locks[kind]:
        ent = _load(kind)
        if not ent or now - ent.get("checked", 0) > ttl:
            ent = fetch(ent or {}, now)
            _save(kind, ent)
        return ent


def _err(e: Exception) -> str:
    code = getattr(e, "code", None)
    return f"HTTP {code}" if code else f"{type(e).__name__}: {e}"[:120]


# --- earthquakes --------------------------------------------------------------------------------------------------

def _km(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 12742 * math.asin(min(1.0, math.sqrt(h)))


def parse_usgs(doc: dict) -> list[dict]:
    out = []
    for f in doc.get("features") or []:
        p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
        if p.get("mag") is None or len(g) < 2 or p.get("type", "earthquake") != "earthquake":
            continue
        out.append({"id": f.get("id"), "t": (p.get("time") or 0) / 1000, "lat": g[1], "lon": g[0],
                    "depth_km": g[2] if len(g) > 2 else None, "mag": round(float(p["mag"]), 1),
                    "mag_type": p.get("magType"), "place": p.get("place") or "", "src": "USGS",
                    "felt": p.get("felt"), "tsunami": bool(p.get("tsunami")), "alert": p.get("alert")})
    return out


def parse_emsc(doc: dict) -> list[dict]:
    out = []
    for f in doc.get("features") or []:
        p = f.get("properties") or {}
        if p.get("mag") is None or p.get("lat") is None or p.get("evtype", "ke") not in ("ke", ""):
            continue   # ke = known earthquake (not a mine blast or an induced event)
        try:
            t = datetime.fromisoformat(p["time"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, AttributeError):
            continue
        out.append({"id": f"emsc-{p.get('unid') or p.get('source_id')}", "t": t, "lat": p["lat"], "lon": p["lon"],
                    "depth_km": p.get("depth"), "mag": round(float(p["mag"]), 1), "mag_type": p.get("magtype"),
                    "place": (p.get("flynn_region") or "").title(), "src": "EMSC", "felt": None, "tsunami": False,
                    "alert": None})
    return out


def merge_quakes(usgs: list[dict], emsc: list[dict], now: float, keep_days: float = 7) -> list[dict]:
    """USGS first; an EMSC event within 60 s and 100 km of a USGS one is the same quake. Newest first."""
    seen = {q["id"]: q for q in usgs if q.get("id")}
    out = sorted(seen.values(), key=lambda q: q["t"])
    times = [q["t"] for q in out]
    for e in emsc:   # only USGS quakes within a minute of this one can be the same event
        lo, hi = bisect.bisect_left(times, e["t"] - 60), bisect.bisect_right(times, e["t"] + 60)
        if any(_km(e["lat"], e["lon"], u["lat"], u["lon"]) < 100 for u in out[lo:hi]):
            continue
        out.append(e)
    cutoff = now - keep_days * 86400
    return sorted((q for q in out if q["t"] >= cutoff), key=lambda q: -q["t"])


def _fetch_quakes(old: dict, now: float) -> dict:
    errors, sources, usgs, emsc = {}, [], [], []
    try:   # every size for a day (dense in the US), and M2.5+ for the week (worldwide)
        byid = {}
        for feed in ("all_day", "2.5_week"):
            for q in parse_usgs(json.loads(get(USGS.format(feed), timeout=30, tries=2))):
                byid[q["id"]] = q
        usgs = list(byid.values())
        sources.append("USGS")
    except Exception as e:  # noqa: BLE001 - keep the old USGS part
        errors["usgs"] = _err(e)
        usgs = [q for q in old.get("quakes") or [] if q["src"] == "USGS"]
    try:
        start = (datetime.fromtimestamp(now, UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
        q = urlencode({"start": start, "minmag": 2, "limit": 3000, "format": "json"})
        emsc = parse_emsc(json.loads(get(f"{EMSC}?{q}", timeout=30, tries=2)))
        sources.append("EMSC")
    except Exception as e:  # noqa: BLE001
        errors["emsc"] = _err(e)
        emsc = [q for q in old.get("quakes") or [] if q["src"] == "EMSC"]
    quakes = merge_quakes(usgs, emsc, now)
    if errors:
        log.warning("quakes: %s", errors)
    log.info("quakes: %d (%s)", len(quakes), ", ".join(sources) or "cached")
    return {"fetched": now if sources else old.get("fetched", 0), "checked": now,
            "error": "; ".join(f"{k} {v}" for k, v in errors.items()) or None, "sources": sources, "quakes": quakes}


def quakes(now: float | None = None) -> dict:
    return _cached("quakes", QUAKE_TTL, _fetch_quakes, now)


# --- wildfires ----------------------------------------------------------------------------------------------------

def _arcgis(layer: str, where: str, fields: str, geometry: bool, extra: dict | None = None) -> list[dict]:
    q = {"where": where, "outFields": fields, "returnGeometry": str(geometry).lower(), "outSR": 4326, "f": "json"}
    doc = json.loads(get(f"{NIFC}/{layer}/FeatureServer/0/query?{urlencode(q | (extra or {}))}", timeout=60, tries=2))
    if doc.get("error"):
        raise RuntimeError(str(doc["error"].get("message") or doc["error"])[:120])
    return doc.get("features") or []


def parse_nifc_incidents(feats: list[dict], now: float) -> list[dict]:
    out = []
    for f in feats:
        a, g = f.get("attributes") or {}, f.get("geometry") or {}
        if g.get("x") is None or g.get("y") is None:
            continue
        pct = a.get("PercentContained")
        upd = (a.get("ModifiedOnDateTime_dt") or 0) / 1000
        if pct is not None and pct >= 100 and now - upd > CONTAINED_KEEP_DAYS * 86400:
            continue   # contained and quiet for days: still "current" to NIFC until declared out, but not news
        out.append({"id": f"nifc-{a.get('IrwinID') or a.get('IncidentName')}-{round(g['x'], 3)}",
                    "name": (a.get("IncidentName") or "UNNAMED").strip().upper(), "lat": g["y"], "lon": g["x"],
                    "acres": a.get("IncidentSize"), "ha": None, "contained": pct,
                    "discovered": (a.get("FireDiscoveryDateTime") or 0) / 1000 or None, "updated": upd or None,
                    "src": "NIFC", "country": "US"})
    return out


def parse_nifc_perimeters(feats: list[dict], names: set[str]) -> list[dict]:
    out = []
    for f in feats:
        a, g = f.get("attributes") or {}, f.get("geometry") or {}
        name = (a.get("attr_IncidentName") or a.get("poly_IncidentName") or "").strip().upper()
        rings = [[[round(x, 4), round(y, 4)] for x, y in r] for r in g.get("rings") or [] if len(r) >= 3]
        if rings and (not names or name in names):
            out.append({"name": name, "acres": a.get("poly_GISAcres"), "contained": a.get("attr_PercentContained"),
                        "rings": rings})
    return out


def parse_gdacs(doc: dict) -> list[dict]:
    out = []
    for f in doc.get("features") or []:
        p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
        if p.get("eventtype") != "WF" or len(g) < 2 or str(p.get("iscurrent")).lower() != "true":
            continue
        if "United States" in (p.get("country") or ""):
            continue   # NIFC covers the US in far more detail
        sev = p.get("severitydata") or {}
        ha = sev.get("severity") if (sev.get("severityunit") or "").lower() == "ha" else None

        def ts(s):
            try:
                return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()
            except (TypeError, ValueError):
                return None

        out.append({"id": f"gdacs-{p.get('eventid')}-{p.get('episodeid')}", "name": "WILDFIRE",
                    "lat": g[1], "lon": g[0], "acres": round(ha * ACRES_PER_HA) if ha else None, "ha": ha,
                    "contained": None, "discovered": ts(p.get("fromdate")), "updated": ts(p.get("todate")),
                    "src": "GDACS", "country": p.get("country") or "", "alert": (p.get("alertlevel") or "").upper()})
    return out


def _fetch_fires(old: dict, now: float) -> dict:
    errors, sources = {}, []
    fires, perims = [], []
    try:
        feats = _arcgis("WFIGS_Incident_Locations_Current", "IncidentTypeCategory='WF'",
                        "IncidentName,IncidentSize,PercentContained,FireDiscoveryDateTime,ModifiedOnDateTime_dt,"
                        "IrwinID", True)
        nifc = parse_nifc_incidents(feats, now)
        names = {f["name"] for f in nifc}
        try:
            pf = _arcgis("WFIGS_Interagency_Perimeters_Current", "attr_IncidentTypeCategory='WF'",
                         "poly_IncidentName,attr_IncidentName,poly_GISAcres,attr_PercentContained", True,
                         {"maxAllowableOffset": 0.001, "geometryPrecision": 4})
            perims = parse_nifc_perimeters(pf, names)
        except Exception as e:  # noqa: BLE001 - incidents without outlines are still useful
            errors["perimeters"] = _err(e)
            perims = old.get("perimeters") or []
        fires += nifc
        sources.append("NIFC")
    except Exception as e:  # noqa: BLE001
        errors["nifc"] = _err(e)
        fires += [f for f in old.get("fires") or [] if f["src"] == "NIFC"]
        perims = old.get("perimeters") or []
    try:
        start = (datetime.fromtimestamp(now, UTC) - timedelta(days=14)).strftime("%Y-%m-%d")
        q = urlencode({"eventlist": "WF", "fromDate": start, "alertlevel": "green;orange;red"})
        fires += parse_gdacs(json.loads(get(f"{GDACS}?{q}", timeout=30, tries=2)))
        sources.append("GDACS")
    except Exception as e:  # noqa: BLE001
        errors["gdacs"] = _err(e)
        fires += [f for f in old.get("fires") or [] if f["src"] == "GDACS"]
    if errors:
        log.warning("fires: %s", errors)
    log.info("fires: %d, %d perimeters (%s)", len(fires), len(perims), ", ".join(sources) or "cached")
    return {"fetched": now if sources else old.get("fetched", 0), "checked": now,
            "error": "; ".join(f"{k} {v}" for k, v in errors.items()) or None, "sources": sources,
            "fires": fires, "perimeters": perims}


def fires(now: float | None = None) -> dict:
    return _cached("fires", FIRE_TTL, _fetch_fires, now)
