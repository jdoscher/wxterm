"""Spacecraft over the map: orbits from CelesTrak (via the collector), positions computed here with SGP4.

The collector hands over orbital elements a few times a day (/api/tle); everything else is local arithmetic, so the
space layer costs no network traffic while it runs. Three jobs, cheapest first:

- now(): where the spacecraft near the map are this second, with a short ground track ahead and behind
- candidates(): every 30 s, which of the ~9,000 objects will cross the map in the next minute (the only ones now()
  has to work out)
- next_over_map() and passes(): when a space station next crosses the map, and when it can be seen from home
  (sunlit, in a dark sky, at least 10° up)
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

import numpy as np
from sgp4 import omm
from sgp4.api import Satrec, SatrecArray
from sgp4.propagation import gstime

GROUPS = ("stations", "visual", "starlink")    # CelesTrak groups: space stations, ~150 brightest, Starlink
STATIONS = {25544: "ISS", 48274: "TIANGONG"}   # NORAD ids of the crewed stations (ISS (ZARYA), CSS (TIANHE))
RE = 6378.137                                  # WGS84 equatorial radius, km
F = 1 / 298.257223563
E2 = F * (2 - F)
KM_PER_DEG = 111.32
TRACK_S = 90                                   # ground track drawn this many seconds behind and ahead


def jd_parts(t):
    """Unix time(s) -> (jd, fraction) as sgp4 wants them."""
    jd = np.asarray(t, dtype=float) / 86400.0 + 2440587.5
    whole = np.floor(jd - 0.5) + 0.5
    return whole, jd - whole


def geodetic(r, t):
    """TEME positions r (..., 3) in km at unix times t (broadcastable) -> lat, lon (degrees) and altitude (km)."""
    jd, fr = jd_parts(t)
    th = np.vectorize(gstime)(jd + fr)
    c, s = np.cos(th), np.sin(th)
    x = c * r[..., 0] + s * r[..., 1]
    y = -s * r[..., 0] + c * r[..., 1]
    z = r[..., 2]
    lon = np.degrees(np.arctan2(y, x))
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - E2))
    for _ in range(3):   # converges to well under a metre in three steps
        n = RE / np.sqrt(1 - E2 * np.sin(lat) ** 2)
        lat = np.arctan2(z + E2 * n * np.sin(lat), p)
    n = RE / np.sqrt(1 - E2 * np.sin(lat) ** 2)
    alt = p / np.cos(lat) - n
    return np.degrees(lat), lon, alt


def ecef_obs(lat, lon, h_km=0.0):
    la, lo = math.radians(lat), math.radians(lon)
    n = RE / math.sqrt(1 - E2 * math.sin(la) ** 2)
    return np.array([(n + h_km) * math.cos(la) * math.cos(lo), (n + h_km) * math.cos(la) * math.sin(lo),
                     (n * (1 - E2) + h_km) * math.sin(la)])


def teme_to_ecef(r, t):
    jd, fr = jd_parts(t)
    th = np.vectorize(gstime)(jd + fr)
    c, s = np.cos(th), np.sin(th)
    return np.stack([c * r[..., 0] + s * r[..., 1], -s * r[..., 0] + c * r[..., 1], r[..., 2]], axis=-1)


def sun_teme(t):
    """Unit vector to the Sun at unix time(s) t, in the equatorial frame of date (low precision, ~0.01°)."""
    d = np.asarray(t, dtype=float) / 86400.0 + 2440587.5 - 2451545.0
    g = np.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = np.radians(q + 1.915 * np.sin(g) + 0.020 * np.sin(2 * g))
    eps = np.radians(23.439 - 0.00000036 * d)
    return np.stack([np.cos(lam), np.cos(eps) * np.sin(lam), np.sin(eps) * np.sin(lam)], axis=-1)


def look(obs_lat, obs_lon, ecef):
    """Elevation and azimuth (degrees) of ECEF point(s) seen from the observer."""
    o = ecef_obs(obs_lat, obs_lon)
    d = ecef - o
    la, lo = math.radians(obs_lat), math.radians(obs_lon)
    e = -math.sin(lo) * d[..., 0] + math.cos(lo) * d[..., 1]
    n = -math.sin(la) * math.cos(lo) * d[..., 0] - math.sin(la) * math.sin(lo) * d[..., 1] + math.cos(la) * d[..., 2]
    u = math.cos(la) * math.cos(lo) * d[..., 0] + math.cos(la) * math.sin(lo) * d[..., 1] + math.sin(la) * d[..., 2]
    el = np.degrees(np.arctan2(u, np.hypot(e, n)))
    az = (np.degrees(np.arctan2(e, n)) + 360) % 360
    return el, az


def compass(az: float) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[round(az / 45) % 8]


@dataclass
class Craft:
    norad: int
    name: str        # short, for the map label
    station: bool


def short_name(norad: int, name: str) -> str:
    if norad in STATIONS:
        return STATIONS[norad]
    name = re.sub(r"\s*\[[^\]]*\]", "", name).strip().upper()   # "STARLINK-11146 [DTC]": CelesTrak's tags
    for long, short in (("STARLINK-", "STARLINK "), (" (ZARYA)", ""), (" ROCKET BODY", " R/B")):
        name = name.replace(long, short)
    return name[:18]


@dataclass
class Pass:
    name: str
    rise: float          # unix time it climbs past 10°
    peak: float
    set: float
    max_el: float
    rise_dir: str
    set_dir: str
    visible: bool        # sunlit while the sky at home is dark


class Sky:
    """Orbits for every spacecraft we track, and the arithmetic to place them."""

    def __init__(self):
        self.crafts: list[Craft] = []
        self.sats: list[Satrec] = []
        self.array: SatrecArray | None = None
        self.loaded: dict[str, float] = {}            # group -> when its elements were fetched (collector's time)
        self._by_group: dict[str, list[dict]] = {}
        self.cand: list[int] = []                     # indices that may be on the map in the next minute
        self.stations: list[int] = []

    def __len__(self):
        return len(self.sats)

    def load(self, group: str, records: list[dict], fetched: float):
        """Replace one group's elements, then rebuild the combined list (deduplicated by NORAD id)."""
        self._by_group[group] = records
        self.loaded[group] = fetched
        seen, crafts, sats = set(), [], []
        for g in GROUPS:
            for rec in self._by_group.get(g, []):
                try:
                    nid = int(rec["NORAD_CAT_ID"])
                    if nid in seen:
                        continue
                    sat = Satrec()
                    omm.initialize(sat, rec)
                except (KeyError, ValueError, TypeError):
                    continue
                seen.add(nid)
                crafts.append(Craft(nid, short_name(nid, rec.get("OBJECT_NAME", "")), nid in STATIONS))
                sats.append(sat)
        self.crafts, self.sats = crafts, sats
        self.array = SatrecArray(sats) if sats else None
        self.stations = [i for i, c in enumerate(crafts) if c.station]
        self.cand = []

    def propagate(self, idx: list[int], times) -> tuple[np.ndarray, np.ndarray]:
        """TEME positions (len(idx), len(times), 3) and a mask of good results."""
        times = np.atleast_1d(np.asarray(times, dtype=float))
        if not idx:
            return np.zeros((0, len(times), 3)), np.zeros((0, len(times)), bool)
        arr = self.array if len(idx) == len(self.sats) else SatrecArray([self.sats[i] for i in idx])
        jd, fr = jd_parts(times)
        e, r, _ = arr.sgp4(jd, fr)
        return r, (e == 0) & np.isfinite(r).all(axis=-1)

    def _inside(self, idx, times, bbox, pad_km):
        lon0, lon1, lat0, lat1 = bbox
        r, ok = self.propagate(idx, times)
        lat, lon, _ = geodetic(r, np.broadcast_to(times, r.shape[:2]))
        dlat = pad_km / KM_PER_DEG
        dlon = dlat / max(0.2, math.cos(math.radians((lat0 + lat1) / 2)))
        return ok & (lat >= lat0 - dlat) & (lat <= lat1 + dlat) & (lon >= lon0 - dlon) & (lon <= lon1 + dlon)

    def candidates(self, bbox, t0: float, window: float = 60, step: float = 2) -> list[int]:
        """Spacecraft whose ground point is on (or just off) the map at any time in [t0, t0 + window]. Run every 30 s
        in a worker; a step of 2 s is ~15 km at orbital speed, smaller than the smallest map."""
        if not self.sats:
            return []
        times = t0 + np.arange(0, window + step, step)
        inside = self._inside(list(range(len(self.sats))), times, bbox, 20)
        return [int(i) for i in np.nonzero(inside.any(axis=1))[0]]

    def now(self, idx: list[int], t: float, bbox) -> list[tuple[Craft, float, float, float, list]]:
        """(craft, lat, lon, alt km, ground track [(lat, lon), ...]) for each of idx that is on the map at t."""
        if not idx:
            return []
        times = t + np.arange(-TRACK_S, TRACK_S + 1, 5.0)
        r, ok = self.propagate(idx, times)
        lat, lon, alt = geodetic(r, np.broadcast_to(times, r.shape[:2]))
        mid = len(times) // 2
        lon0, lon1, lat0, lat1 = bbox
        out = []
        for k, i in enumerate(idx):
            if not ok[k, mid] or not (lat0 <= lat[k, mid] <= lat1 and lon0 <= lon[k, mid] <= lon1):
                continue
            track = [(float(lat[k, j]), float(lon[k, j])) for j in range(len(times)) if ok[k, j]]
            out.append((self.crafts[i], float(lat[k, mid]), float(lon[k, mid]), float(alt[k, mid]), track))
        # modules and visiting ships docked to a station are tracked as separate objects: fold them into it
        st = [(a, b) for c, a, b, _, _ in out if c.station]
        return [o for o in out if o[0].station or not any(abs(o[1] - a) < 0.1 and abs(o[2] - b) < 0.1 for a, b in st)]

    def next_over_map(self, bbox, t0: float, horizon: float = 24 * 3600, step: float = 2) -> tuple[str, float] | None:
        """(station name, unix time) of the next crewed station to cross the map, or None within the horizon."""
        if not self.stations:
            return None
        best = None
        chunk = 3 * 3600   # keep the arrays small
        for start in np.arange(t0, t0 + horizon, chunk):
            times = start + np.arange(0, chunk, step)
            inside = self._inside(self.stations, times, bbox, 5)
            for k, i in enumerate(self.stations):
                hit = np.nonzero(inside[k])[0]
                if len(hit) and (best is None or times[hit[0]] < best[1]):
                    best = (self.crafts[i].name, float(times[hit[0]]))
            if best:
                return best
        return None

    def passes(self, home, t0: float, horizon: float = 48 * 3600, step: float = 10, min_el: float = 10) -> list[Pass]:
        """Passes of the crewed stations over home, soonest first. visible = the station is in sunlight at some point
        above min_el while the Sun is at least 6° below home's horizon (the sky is dark enough to see it)."""
        if not self.stations:
            return []
        times = t0 + np.arange(0, horizon, step)
        r, ok = self.propagate(self.stations, times)
        ecef = teme_to_ecef(r, np.broadcast_to(times, r.shape[:2]))
        el, az = look(home[0], home[1], ecef)
        sun = sun_teme(times)                                         # (T, 3)
        # Earth's shadow as a cylinder: dark if behind Earth (r·s < 0) and within one radius of the Sun line
        rs = np.einsum("stk,tk->st", r, sun)
        perp = np.linalg.norm(r - rs[..., None] * sun[None], axis=-1)
        lit = (rs > 0) | (perp > RE)
        sun_el, _ = look(home[0], home[1], teme_to_ecef(sun[None] * 1.496e8, times[None]))
        dark = sun_el[0] < -6
        out = []
        for k, i in enumerate(self.stations):
            up = ok[k] & (el[k] >= min_el)
            j = 0
            while j < len(times):
                if not up[j]:
                    j += 1
                    continue
                a = j
                while j < len(times) and up[j]:
                    j += 1
                b = j - 1
                pk = a + int(np.argmax(el[k, a:b + 1]))
                vis = bool((lit[k, a:b + 1] & dark[a:b + 1]).any())
                out.append(Pass(self.crafts[i].name, float(times[a]), float(times[pk]), float(times[b]),
                                float(el[k, pk]), compass(az[k, a]), compass(az[k, b]), vis))
        return sorted(out, key=lambda p: p.rise)


def fmt_until(t: float, now: float | None = None) -> str:
    s = max(0, int(t - (now or time.time())))
    if s < 3600:
        return f"{max(1, s // 60)} MIN"
    if s < 86400:
        return f"{s // 3600}H {s % 3600 // 60:02d}M"
    return f"{s // 86400}D {s % 86400 // 3600}H"
