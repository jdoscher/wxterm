"""wxcollect: poll free weather sources on a schedule, cache the last good result, serve JSON on :8091.

GET /api/all            everything except radar, with per-source fetched/ok/error times
GET /api/radar?w=&h=    radar level grids for a w x h dot map (optional &bbox=lon0,lon1,lat0,lat1, &last=N frames)
GET /api/geo            coastline/border/roads/lakes polylines, town labels, home point
GET /api/aprs           APRS stations heard on APRS-IS around home (positions, symbols, tracks, weather)
GET /healthz
"""
from __future__ import annotations

import gzip
import json
import logging
import random
import threading
import time
import traceback
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import aprs, config, sources, world
from .radar import Radar, bbox_for

log = logging.getLogger("wxcollect")
RADAR = Radar()

_G = config.PROVIDER == "global"


def _no_tides() -> dict:
    return {"station": None, "curve": [], "hilo": [], "none": True}


JOBS = {  # name: (function, interval seconds before WX_POLL_SCALE)
    "obs": (world.fetch_obs if _G else sources.fetch_obs, 300),
    "forecast": (world.fetch_forecast if _G else sources.fetch_forecast, 1800),
    "alerts": (world.fetch_alerts if _G else sources.fetch_alerts, 120),
    "afd": (world.fetch_afd if _G else sources.fetch_afd, 1800),
    "openmeteo": (sources.fetch_openmeteo, 900),
    "marine": (sources.fetch_marine, 1800),
    "transect": (sources.fetch_transect, 1800),
    "tides": ({"noaa": sources.fetch_tides, "model": world.fetch_model_tides}.get(config.TIDE_SOURCE, _no_tides),
              6 * 3600),
    "water_temp": (sources.fetch_water_temp, 900),
    "radar": (RADAR.refresh, 300),
}
if config.APRS:  # the APRS-IS connection is its own thread (aprs.start); this job publishes its status
    JOBS["aprs"] = (aprs.summary, 30)
WAKE = {name: threading.Event() for name in JOBS}  # set by /api/refresh to run a job now
_last_refresh = 0.0

STORE: dict[str, dict] = {}
LOCK = threading.Lock()
CACHE_FILE = Path(config.DATA_DIR) / "cache.json"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def save():
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        with LOCK:
            tmp.write_text(json.dumps(STORE | {"_loc": [config.NAME, config.LAT, config.LON]}))
        tmp.replace(CACHE_FILE)
    except OSError as e:
        log.warning("cache save failed: %s", e)


def load():
    try:
        saved = json.loads(CACHE_FILE.read_text())
        if saved.get("_loc") != [config.NAME, config.LAT, config.LON]:
            log.info("cache is for another location; starting fresh")
            return
        STORE.update(saved)
        if not config.APRS:  # APRS was on last run, not now
            STORE.pop("aprs", None)
        log.info("loaded cache: %s", ", ".join(STORE))
    except (OSError, ValueError):
        pass


def run_job(name: str):
    fn, interval = JOBS[name]
    interval = int(interval * config.POLL_SCALE) if interval < 6 * 3600 else interval
    time.sleep(random.uniform(0, 60 if name == "radar" else 5))  # spread the radar burst across collectors
    while True:
        t0 = time.time()
        try:
            data = fn()
            with LOCK:
                STORE[name] = {"data": data, "ok_at": now_iso(), "error": None, "interval": interval}
            log.info("%s ok (%.1fs)", name, time.time() - t0)
            save()
            wait = interval
        except Exception as e:  # noqa: BLE001 - keep last good data, record the error
            with LOCK:
                ent = STORE.setdefault(name, {"data": None, "ok_at": None, "interval": interval})
                ent["error"], ent["error_at"] = f"{type(e).__name__}: {e}", now_iso()
            log.warning("%s failed: %s", name, e)
            log.debug(traceback.format_exc())
            wait = min(interval, 120)  # retry sooner after a failure
        WAKE[name].wait(wait)
        WAKE[name].clear()


GEO = json.loads(Path(config.GEO).read_text())


def labels() -> dict:
    office = ""
    if not _G:
        try:
            office = sources.points()["gridId"]
        except Exception:  # noqa: BLE001 - NWS down; label only
            pass
    return {"name": config.NAME, "office": office, "obs_station": config.OBS_STATION,
            "tide_name": config.TIDE_NAME, "water_name": config.WATER_TEMP_NAME, "surf_name": config.SURF_NAME,
            "transect_title": config.TRANSECT_TITLE, "transect_water": config.TRANSECT_WATER,
            "forecast_src": f"NWS {office}".strip() if not _G else "OPEN-METEO",
            "alerts_src": "NWS" if not _G else None, "tide_source": config.TIDE_SOURCE}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet access log
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, separators=(",", ":")).encode()
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 2048
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/refresh":  # display switched to this location: catch up now (max once a minute)
                global _last_refresh
                fresh = time.time() - _last_refresh > 60
                if fresh:
                    _last_refresh = time.time()
                    for ev in WAKE.values():
                        ev.set()
                return self._send({"ok": True, "woke": fresh})
            if u.path == "/healthz":
                return self._send({"ok": True, "time": now_iso()})
            if u.path == "/api/all":
                with LOCK:
                    snap = {k: v for k, v in STORE.items() if k not in ("radar", "_loc")}
                    radar_meta = STORE.get("radar")
                return self._send({
                    "now": now_iso(),
                    "home": {"lat": config.LAT, "lon": config.LON, "tz": config.TZ},
                    "labels": labels(),
                    "sources": snap, "radar_meta": radar_meta, "astro": sources.astro(),
                    "map_span_lon": config.MAP_SPAN_LON,
                })
            if u.path == "/api/radar":
                w, h = int(q.get("w", 240)), int(q.get("h", 152))
                bbox = [float(x) for x in q["bbox"].split(",")] if "bbox" in q else bbox_for(w, h)
                return self._send(RADAR.grid(w, h, bbox, last=int(q.get("last", 0))))
            if u.path == "/api/aprs":
                return self._send(aprs.snapshot() if config.APRS else {"enabled": False, "stations": []})
            if u.path == "/api/geo":
                return self._send(GEO | {"towns": config.TOWNS, "home": [config.LAT, config.LON],
                                         "span_lon": config.MAP_SPAN_LON})
            self._send({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            self._send({"error": repr(e)}, 500)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load()
    if config.APRS:
        # listed in /api/all from the first request, so the display knows APRS is on while it connects
        STORE.setdefault("aprs", {"data": None, "ok_at": None, "error": "connecting", "interval": 30})
        aprs.start()
    for name in JOBS:
        threading.Thread(target=run_job, args=(name,), daemon=True, name=name).start()
    srv = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    log.info("listening on :%d (home %.4f,%.4f)", config.PORT, config.LAT, config.LON)
    srv.serve_forever()


if __name__ == "__main__":
    main()
