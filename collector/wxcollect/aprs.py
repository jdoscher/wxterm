"""APRS for the map: stations heard on APRS-IS around home.

One thread (start()) holds a receive-only connection to APRS-IS: passcode -1, so the servers never accept anything
from us, with a range filter around home (r/lat/lon/km, home rounded to 0.1° so the exact location stays on the
machine). Each packet is parsed with aprsparse (wxterm's own, MIT) and folded into a table of
stations: last position, symbol, comment, a short track, and the latest weather report for weather stations (converted
to °F, mph, inches and mb like the rest of wxterm). Stations not heard for WX_APRS_MAX_AGE_MIN minutes are dropped.
The table is saved to the data dir every few minutes so a restart doesn't begin with an empty map.

summary() runs as the "aprs" job (connection status for /api/all and the status bar); snapshot() answers /api/aprs.
"""
from __future__ import annotations

import json
import logging
import math
import re
import socket
import threading
import time
from pathlib import Path

from . import aprsparse, config

log = logging.getLogger("wxcollect.aprs")

VERSION = "wxterm 1.4"
TRACK = 40          # track points kept per station
MOVE_M = 40         # a new track point only after moving this far, so GPS jitter on a parked car isn't a track
SAVE_EVERY = 300    # seconds between saves of the station table
QUIET = 120         # no line at all (servers send a keepalive every 20 s) for this long = connection is dead
LOGIN = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?$")
RF_Q = ("qAR", "qAr", "qAo", "qAO")   # q constructs added by an igate that heard the packet on the air

FILE = Path(config.DATA_DIR) / "aprs.json"
STATIONS: dict[str, dict] = {}
LOCK = threading.Lock()
STATUS: dict = {"connected": False, "server": "", "since": None, "alive_at": None, "last_packet": None,
                "packets": 0, "verified": None, "error": None}
_last_save = 0.0


def km(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _r(v, nd=1):
    return None if v is None else round(v, nd)


def _wx(w: dict) -> dict:
    """aprslib weather (metric) in wxterm's units. Keys are present only when the station reported them."""
    c = w.get("temperature")
    out = {"temp_f": _r(c * 9 / 5 + 32) if c is not None else None,
           "wind_dir": w.get("wind_direction"),
           "wind_mph": _r(w["wind_speed"] * 2.23694) if w.get("wind_speed") is not None else None,
           "gust_mph": _r(w["wind_gust"] * 2.23694) if w.get("wind_gust") is not None else None,
           "humidity": w.get("humidity"),
           "pressure_mb": _r(w.get("pressure")),
           "rain_1h_in": _r(w["rain_1h"] / 25.4, 2) if w.get("rain_1h") is not None else None,
           "rain_24h_in": _r(w["rain_24h"] / 25.4, 2) if w.get("rain_24h") is not None else None,
           "rain_today_in": _r(w["rain_since_midnight"] / 25.4, 2) if w.get("rain_since_midnight") is not None
           else None}
    return {k: v for k, v in out.items() if v is not None}


def ingest(pkt: dict, now: float) -> str | None:
    """Fold one parsed packet into the station table. Returns the station's name, or None if it was ignored."""
    fmt = pkt.get("format")
    name = (pkt.get("object_name") or pkt.get("item_name") or "").strip() or pkt.get("from") or ""
    if not name or fmt in ("message", "telemetry-message", "bulletin"):
        return None
    with LOCK:
        if fmt in ("object", "item") and pkt.get("alive") is False:   # the owner killed the object or item
            STATIONS.pop(name, None)
            return name
        st = STATIONS.get(name)
        if fmt == "status":
            if st is not None and pkt.get("status"):
                st["status"], st["t"] = pkt["status"].strip()[:80], now
            return name if st is not None else None
        lat, lon = pkt.get("latitude"), pkt.get("longitude")
        has_pos = isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon)
        if has_pos and km(config.LAT, config.LON, lat, lon) > config.APRS_RANGE_KM * 1.5:
            return None   # bad position (or an object placed far away): not ours to draw
        if st is None:
            if not has_pos and "weather" not in pkt:
                return None
            st = STATIONS[name] = {"call": name, "n": 0, "track": []}
        st["t"], st["n"] = now, st["n"] + 1
        path = [p for p in pkt.get("path") or [] if isinstance(p, str)]
        st["path"] = path[:8]
        st["rf"] = any(p in RF_Q for p in path)
        if pkt.get("via"):
            st["via"] = pkt["via"]
        if name != pkt.get("from"):
            st["obj"], st["owner"] = True, pkt.get("from")
        w = dict(pkt.get("weather") or {})
        sym = (pkt.get("symbol_table") or "") + (pkt.get("symbol") or "")
        if has_pos:
            if len(sym) == 2:
                st["sym"] = sym
            if pkt.get("comment") is not None:
                st["comment"] = pkt["comment"].strip()[:80]
            if sym[1:] == "_" and w:
                # a weather station's course/speed are its wind direction and speed, in mph. aprsparse already
                # files them under weather; a parser that reads them as course and knots (km/h) is undone here
                if pkt.get("course") is not None:
                    w.setdefault("wind_direction", pkt["course"])
                if pkt.get("speed") is not None:
                    w.setdefault("wind_speed", pkt["speed"] / 1.852 / 2.23694)
            else:
                st["course"] = pkt.get("course") or None
                st["speed_mph"] = _r(pkt["speed"] * 0.621371) if pkt.get("speed") is not None else None
            st["alt_ft"] = round(pkt["altitude"] * 3.28084) if pkt.get("altitude") is not None else None
            moved = not st.get("lat") or km(st["lat"], st["lon"], lat, lon) * 1000 >= MOVE_M
            st["lat"], st["lon"], st["tpos"] = round(lat, 5), round(lon, 5), now
            if moved:
                st["track"].append([st["lat"], st["lon"], int(now)])
                del st["track"][:-TRACK]
        if w:
            st["wx"], st["wx_t"] = _wx(w), now
    return name


def prune(now: float):
    cut = now - config.APRS_MAX_AGE_MIN * 60
    with LOCK:
        for k in [k for k, s in STATIONS.items() if s.get("t", 0) < cut]:
            del STATIONS[k]


def load():
    try:
        saved = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return
    if saved.get("loc") != [config.LAT, config.LON]:
        return
    with LOCK:
        STATIONS.update(saved.get("stations") or {})
    prune(time.time())
    log.info("aprs: loaded %d stations", len(STATIONS))


def save():
    global _last_save
    _last_save = time.time()
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        with LOCK:
            tmp.write_text(json.dumps({"loc": [config.LAT, config.LON], "stations": STATIONS}))
        tmp.replace(FILE)
    except OSError as e:
        log.warning("aprs: save failed: %s", e)


def summary() -> dict:
    """The "aprs" job: connection status and counts. Raises while disconnected so /api/all shows the error."""
    now = time.time()
    prune(now)
    if now - _last_save > SAVE_EVERY:
        save()
    with LOCK:
        n = sum(1 for s in STATIONS.values() if s.get("lat") is not None)
        nwx = sum(1 for s in STATIONS.values() if s.get("lat") is not None and s.get("wx"))
        st = dict(STATUS)
    if not st["connected"] or not st["alive_at"] or now - st["alive_at"] > QUIET:
        raise RuntimeError(st["error"] or f"connecting to {config.APRS_SERVER}")
    return st | {"call": config.APRS_CALL, "range_km": config.APRS_RANGE_KM, "stations": n, "wx_stations": nwx}


def snapshot() -> dict:
    """Every station with a position, for /api/aprs."""
    now = time.time()
    prune(now)
    with LOCK:
        rows = [dict(s, track=list(s.get("track") or [])) for s in STATIONS.values() if s.get("lat") is not None]
        st = dict(STATUS)
    return {"enabled": True, "now": round(now, 1), "home": [config.LAT, config.LON], "call": config.APRS_CALL,
            "range_km": config.APRS_RANGE_KM,
            "max_age_min": config.APRS_MAX_AGE_MIN, "status": st, "stations": rows}


def filter_text() -> str:
    return config.APRS_FILTER or f"r/{config.LAT:.1f}/{config.LON:.1f}/{int(config.APRS_RANGE_KM)}"


def login_line() -> bytes:
    return f"user {config.APRS_CALL} pass -1 vers {VERSION} filter {filter_text()}\r\n".encode()


def handle_line(raw: bytes, now: float):
    line = raw.rstrip(b"\r\n")
    STATUS["alive_at"] = now
    if not line:
        return
    if line.startswith(b"#"):
        txt = line.decode("latin-1")
        if "logresp" in txt:
            m = re.search(r"server (\S+)", txt)
            STATUS["server"] = m.group(1) if m else STATUS["server"]
            STATUS["verified"] = " verified" in txt
            log.info("aprs: %s", txt[2:])
        elif "not allowed" in txt.lower() or "invalid" in txt.lower():
            raise ConnectionError(txt[2:80])
        return
    try:
        pkt = aprsparse.parse(line)
    except Exception:  # noqa: BLE001 - APRS has a lot of odd packets; skip what we can't read
        return
    if ingest(pkt, now):
        STATUS["packets"] += 1
        STATUS["last_packet"] = now


def session(host: str, port: int):
    """One connection, until it drops. Raises ConnectionError (or socket errors) when it does."""
    with socket.create_connection((host, port), timeout=30) as sock:
        sock.settimeout(QUIET)
        f = sock.makefile("rb")
        if not f.readline():
            raise ConnectionError("closed before login")
        sock.sendall(login_line())
        STATUS.update(connected=True, since=time.time(), alive_at=time.time(), error=None)
        log.info("aprs: connected to %s:%d, filter %s", host, port, filter_text())
        try:
            for raw in f:
                handle_line(raw, time.time())
        finally:
            STATUS["connected"] = False
    raise ConnectionError("server closed the connection")


def run():
    if not LOGIN.match(config.APRS_CALL):
        STATUS["error"] = "SET WX_APRS_CALL (KIOSK.CONF APRS_CALL) TO YOUR CALLSIGN"
        log.warning("aprs: no valid WX_APRS_CALL (%r); not connecting", config.APRS_CALL)
        return
    host, _, port = config.APRS_SERVER.rpartition(":")
    host, port = (host, int(port)) if host and port.isdigit() else (config.APRS_SERVER, 14580)
    backoff = 10
    while True:
        t0 = time.time()
        try:
            session(host, port)
        except Exception as e:  # noqa: BLE001 - log, wait, reconnect
            STATUS.update(connected=False, error=f"{type(e).__name__}: {e}"[:80])
            log.warning("aprs: %s", STATUS["error"])
        backoff = 10 if time.time() - t0 > 120 else min(backoff * 2, 300)
        time.sleep(backoff)


def start():
    load()
    threading.Thread(target=run, daemon=True, name="aprs-is").start()
