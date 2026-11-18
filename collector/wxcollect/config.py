"""Collector settings. Defaults are New York City; override any of them in .env (next to docker-compose.yml).

List settings (WX_TRANSECT, WX_TOWNS) take "NAME:lat:lon;NAME:lat:lon;...".
"""
from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _places(name: str, default: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    out = []
    for item in raw.split(";"):
        n, la, lo = item.split(":")
        out.append((n.strip().upper(), float(la), float(lo)))
    return out


def _opt(name: str) -> float | None:
    v = os.environ.get(name, "").strip()
    return float(v) if v else None


NAME = os.environ.get("WX_NAME", "NEW YORK CITY").upper()
PROVIDER = os.environ.get("WX_PROVIDER", "nws").lower()   # nws (US) | global (METAR + Open-Meteo)
POLL_SCALE = max(0.25, _f("WX_POLL_SCALE", 1.0))          # >1 polls slower (demo cities); /api/refresh catches up
LAT = _f("WX_LAT", 40.7128)
LON = _f("WX_LON", -74.0060)
TZ = os.environ.get("WX_TZ", "America/New_York")
OBS_STATION = os.environ.get("WX_OBS_STATION", "KNYC")             # NWS station id, or ICAO for METAR (global)
TIDE_STATION = os.environ.get("WX_TIDE_STATION", "")               # NOAA CO-OPS id; empty = none (no borrowed default)
TIDE_NAME = os.environ.get("WX_TIDE_NAME", "THE BATTERY").upper()
TIDE_DATUM = os.environ.get("WX_TIDE_DATUM", "MLLW")
TIDE_SOURCE = os.environ.get("WX_TIDE_SOURCE", "noaa" if TIDE_STATION else "none")  # noaa | model | none
TIDE_LAT, TIDE_LON = _opt("WX_TIDE_LAT"), _opt("WX_TIDE_LON")      # model tide point (open water)
WATER_TEMP_STATION = os.environ.get("WX_WATER_TEMP_STATION", TIDE_STATION)  # some tide stations have no sensor
WATER_TEMP_NAME = os.environ.get("WX_WATER_TEMP_NAME", TIDE_NAME).upper()
GEO = os.environ.get("WX_GEO", "/locations/nyc.geo.json")
SURF_LAT = _opt("WX_SURF_LAT")                                      # empty = no surf panel
SURF_LON = _opt("WX_SURF_LON")
SST_LAT = _opt("WX_SST_LAT") or TIDE_LAT or SURF_LAT                 # model water temp when there's no sensor
SST_LON = _opt("WX_SST_LON") or TIDE_LON or SURF_LON
SURF_NAME = os.environ.get("WX_SURF_NAME", "ROCKAWAY BEACH").upper()
MAP_SPAN_LON = _f("WX_MAP_SPAN_LON", 2.6)                           # degrees of longitude across the radar panel
PORT = int(os.environ.get("WX_PORT", 8091))
DATA_DIR = os.environ.get("WX_DATA_DIR", "/data")
# NWS asks every client to identify itself with a contact (URL or email). Set WX_UA (kiosk.conf: NWS_CONTACT).
UA = os.environ.get("WX_UA") or "wxterm/1.5 (+https://wxterm.app)"

# APRS stations heard on APRS-IS around home (aprs.py). Receive-only: passcode -1, nothing is ever transmitted.
# APRS-IS still wants a login name, so set WX_APRS_CALL to your callsign (kiosk.conf APRS_CALL).
APRS = os.environ.get("WX_APRS", "").strip().lower() in ("1", "yes", "true", "on")
APRS_CALL = os.environ.get("WX_APRS_CALL", "").strip().upper()
APRS_SERVER = os.environ.get("WX_APRS_SERVER", "").strip() or "rotate.aprs2.net:14580"
APRS_RANGE_KM = _opt("WX_APRS_RANGE_KM") or 150.0                   # filter radius; 150 km covers the radar map
APRS_MAX_AGE_MIN = _opt("WX_APRS_MAX_AGE_MIN") or 60.0              # drop stations not heard for this long
APRS_FILTER = os.environ.get("WX_APRS_FILTER", "").strip()          # replaces the r/lat/lon/km filter if set

# Shore-to-mountains line for the transect panel, in drawing order (left to right)
TRANSECT_TITLE = os.environ.get("WX_TRANSECT_TITLE", "SHORE TO CATSKILLS").upper()
TRANSECT_WATER = os.environ.get("WX_TRANSECT_WATER", "").upper()   # label for the water at the left end, if any
TRANSECT = _places("WX_TRANSECT", [
    ("ROCKAWAY", 40.583, -73.816),
    ("MANHATTAN", 40.781, -73.966),
    ("YONKERS", 40.931, -73.899),
    ("BEAR MTN", 41.312, -73.988),
    ("MONTICELLO", 41.655, -74.689),
    ("SLIDE MTN", 41.998, -74.386),
])

# Labels for the radar map
TOWNS = _places("WX_TOWNS", [
    ("NEWARK", 40.7357, -74.1724),
    ("PATERSON", 40.9168, -74.1718),
    ("WHITE PLAINS", 41.0340, -73.7629),
    ("STAMFORD", 41.0534, -73.5387),
    ("BRIDGEPORT", 41.1865, -73.1952),
    ("NEW HAVEN", 41.3083, -72.9279),
    ("DANBURY", 41.3948, -73.4540),
    ("POUGHKEEPSIE", 41.7004, -73.9210),
    ("MORRISTOWN", 40.7968, -74.4815),
    ("NEW BRUNSWICK", 40.4862, -74.4518),
    ("TRENTON", 40.2206, -74.7597),
    ("ASBURY PARK", 40.2204, -74.0121),
    ("HEMPSTEAD", 40.7062, -73.6187),
    ("ISLIP", 40.7298, -73.2104),
    ("PORT JEFFERSON", 40.9465, -73.0693),
    ("ALLENTOWN", 40.6023, -75.4714),
])
