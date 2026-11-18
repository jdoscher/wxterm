"""The big map (MapPanel) and its air traffic layer. Air traffic by Justin Van Bibber, ported from AsciiFlights.

The map is line art (coast, borders, highways, towns, runways and airport codes) centred on home at a range you
choose, with two layers you turn on and off: the radar loop (`w`) and live air traffic (`a`). Each aircraft is an
arrow pointing along its track with a short braille trail, and can carry a data tag (callsign, type, route or
registration, altitude, climb/descent, ground speed) on a leader line. Between polls, positions are dead-reckoned so
traffic moves smoothly. When the panel is wide enough, a traffic list and the selected aircraft's details sit beside
the map.

Data comes from the collector: /api/aircraft (adsb.fi, adsb.lol) and /api/routes (adsbdb).
"""
from __future__ import annotations

import math
import re
import time
from collections import deque

from datetime import datetime

from .canvas import BAYER, Canvas
from . import panels
from .panels import DENS, Panel, hm
from .space import fmt_until

RANGES = (5, 10, 15, 25, 40, 60, 100)   # NM from home to the nearest map edge; map lines reach about 100 NM
DEFAULT_RANGE = 40                      # about the area the radar-only map used to show
TAG_MODES = ("AUTO", "ALL", "OFF")
ARROWS = "↑↗→↘↓↙←↖"
R_NM = 3440.065
WX_DENS = (0, 1, 2, 3, 4, 5)            # radar under air traffic is sparser, so aircraft stand out
AIRLINE = re.compile(r"^[A-Z]{3}\d")      # ICAO airline callsigns (UAL123) have routes; tail numbers don't


# --- geometry: degrees and nautical miles ------------------------------------------------------------------------

def dist_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_NM * math.asin(min(1.0, math.sqrt(h)))


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def dest(lat, lon, brg, nm) -> tuple[float, float]:
    d, b, p1, l1 = nm / R_NM, math.radians(brg), math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def xtrack_nm(lat1, lon1, lat2, lon2, lat3, lon3) -> float:
    """Distance of point 3 from the great circle through points 1 and 2."""
    d13 = dist_nm(lat1, lon1, lat3, lon3) / R_NM
    a = math.radians(bearing(lat1, lon1, lat3, lon3) - bearing(lat1, lon1, lat2, lon2))
    return abs(math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(a)))) * R_NM)


def ring_step(rng: float) -> float:
    for s in (1, 2, 2.5, 5, 10, 20, 25, 50):
        if rng / s <= 3:
            return s
    return 100


# --- the traffic picture ------------------------------------------------------------------------------------------

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class Plane:
    def __init__(self, hex_: str):
        self.hex = hex_
        self.trail: deque = deque(maxlen=900)   # (lat, lon, time, alt)
        self.route_ok: tuple[str, bool] | None = None
        self.last_seen = 0.0

    def update(self, a: dict, now: float, t_resp: float):
        self.flight = (a.get("flight") or "").upper()
        self.reg = (a.get("r") or "").upper()
        self.type = (a.get("t") or "").upper()
        self.desc = (a.get("desc") or "").upper()
        self.op = (a.get("ownOp") or "").upper()
        self.squawk = a.get("squawk") or ""
        self.emergency = (a.get("emergency") or "none") != "none" or self.squawk in ("7500", "7600", "7700")
        heads = [_num(a.get(k)) for k in ("track", "true_heading", "mag_heading")]
        self.track = next((v for v in heads if v is not None), None)
        self.gs = _num(a.get("gs"))
        self.ground = a.get("alt_baro") == "ground"
        self.alt = 0 if self.ground else _num(a.get("alt_baro")) if _num(a.get("alt_baro")) is not None \
            else _num(a.get("alt_geom"))
        self.vr = _num(a.get("baro_rate")) if _num(a.get("baro_rate")) is not None else _num(a.get("geom_rate"))
        self.nav_alt = _num(a.get("nav_altitude_mcp"))
        kind = a.get("type") or ""   # readsb's message source: adsb_icao, mlat, tisb_other, adsr_icao, mode_s, ...
        names = (("mlat", "MLAT"), ("tisb", "TIS-B"), ("adsr", "ADS-R"), ("adsc", "ADS-C"), ("mode_s", "MODE S"))
        self.src = next((name for pre, name in names if kind.startswith(pre)),
                        "MLAT" if not kind and a.get("mlat") else "ADS-B")
        self.seen = _num(a.get("seen"))
        self.lat, self.lon = a["lat"], a["lon"]
        self.t_pos = t_resp - (_num(a.get("seen_pos")) or 0)
        if not self.trail or self.trail[-1][:2] != (self.lat, self.lon):
            self.trail.append((self.lat, self.lon, self.t_pos, self.alt))
        while self.trail and now - self.trail[0][2] > 1800:
            self.trail.popleft()
        self.last_seen = now

    @property
    def callsign(self) -> str:
        return self.flight or self.reg or self.hex.upper()

    def pos(self, now: float) -> tuple[float, float]:
        """Dead-reckoned position: last report plus speed along track, for at most 20 s (6 s on the ground)."""
        dt = now - self.t_pos
        if self.gs is None or self.track is None or self.gs < 1 or dt <= 0:
            return self.lat, self.lon
        return dest(self.lat, self.lon, self.track, self.gs * min(dt, 6 if self.ground else 20) / 3600)


class Traffic:
    """Aircraft, routes and feed status for the air traffic layer. Lives on the app; the panel only reads it."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.planes: dict[str, Plane] = {}
        self.routes: dict[str, dict | None | str] = {}   # callsign -> route, None (unknown) or "pending"
        self.home: tuple[float, float] | None = None
        self.source, self.error, self.ok_at = "", "", 0.0
        self.selected: str | None = None
        self.tags_full, self.tags_pending = True, None    # AUTO mode: all tags shown, or only the selected one
        self.placed: dict[str, str] = {}                  # hex -> tag spot last frame, so tags don't hop around

    def ingest(self, j: dict, now: float):
        t_resp = _num(j.get("now")) or now
        seen = set()
        for a in j.get("ac") or []:
            hx = a.get("hex")
            if not hx or _num(a.get("lat")) is None or _num(a.get("lon")) is None:
                continue
            self.planes.setdefault(hx, Plane(hx)).update(a, now, t_resp)
            seen.add(hx)
        for hx in [h for h, p in self.planes.items() if h not in seen and now - p.last_seen > 25]:
            del self.planes[hx]
            self.placed.pop(hx, None)
            if self.selected == hx:
                self.selected = None
        if j.get("home"):
            self.home = tuple(j["home"])
        self.source, self.error, self.ok_at = j.get("source") or "ADS-B", "", now

    def route(self, p: Plane) -> dict | None:
        """The route on file for this flight, if it fits where the aircraft is and where it's heading.
        Route databases go stale (schedules change, callsigns get reused), and a wrong route misleads more than
        no route."""
        r = self.routes.get(p.flight)
        if not isinstance(r, dict):
            return None
        if p.route_ok is None or p.route_ok[0] != p.flight:
            p.route_ok = (p.flight, _plausible(r, p))
        return r if p.route_ok[1] else None

    def route_text(self, p: Plane) -> str:
        r = self.route(p)
        return f"{_ap(r['origin'])}›{_ap(r['dest'])}" if r else ""

    def wanted_routes(self, planes, limit: int = 8) -> list[str]:
        out = []
        for p in planes:
            if p.flight and AIRLINE.match(p.flight) and p.flight not in self.routes and p.flight not in out:
                out.append(p.flight)
                if len(out) >= limit:
                    break
        return out


def _ap(a: dict | None) -> str:
    return (a or {}).get("iata") or (a or {}).get("icao") or "???"


def _plausible(r: dict, p: Plane) -> bool:
    o, d = r.get("origin") or {}, r.get("dest") or {}
    if None in (o.get("lat"), o.get("lon"), d.get("lat"), d.get("lon")):
        return False
    span = dist_nm(o["lat"], o["lon"], d["lat"], d["lon"])
    d_o, d_d = dist_nm(o["lat"], o["lon"], p.lat, p.lon), dist_nm(d["lat"], d["lon"], p.lat, p.lon)
    if xtrack_nm(o["lat"], o["lon"], d["lat"], d["lon"], p.lat, p.lon) > max(150, span * 0.3) or \
            d_o + d_d > span * 1.6 + 200:
        return False
    if p.ground:
        return True
    # away from both ends, an aircraft flies roughly toward its destination; if not, the route is someone else's
    if p.track is not None and d_o > 30 and d_d > 80:
        off = abs((bearing(p.lat, p.lon, d["lat"], d["lon"]) - p.track + 540) % 360 - 180)
        if off > 110:
            return False
    # climbing hard near the "destination", or descending into the "origin": it's the return leg
    if p.vr is not None and ((d_d < 80 < d_o and p.vr > 1000) or (d_o < 80 < d_d and p.vr < -1000)):
        return False
    return True


# --- text --------------------------------------------------------------------------------------------------------

def alt_text(p: Plane, short: bool = False) -> str:
    if p.ground:
        return "GND"
    if p.alt is None:
        return "---" if short else "-----"
    if p.alt >= 18000:
        return f"FL{round(p.alt / 100):03d}"
    return f"{round(p.alt / 100) * 100}" if short else f"{round(p.alt / 25) * 25}FT"


def vr_glyph(p: Plane) -> str:
    if p.ground or p.vr is None:
        return " "
    return "▲" if p.vr > 300 else "▼" if p.vr < -300 else " "


def spd(p: Plane) -> str:
    return "---" if p.gs is None else f"{p.gs:.0f}"


def glyph(p: Plane) -> str:
    return "•" if p.track is None else ARROWS[round(p.track % 360 / 45) % 8]


# --- data tags: candidate spots around an aircraft ---------------------------------------------------------------
# A leader runs out 1-4 cells in one of eight directions and the box can slide along it, so busy skies still find
# room. Each spot: (cost, key, leader cells, leader glyph, box x, box y, junction (x, y, glyph) or None).

BW, BH, IW = 18, 4, 14


def _spots() -> list[tuple]:
    out = []

    def add(d, ln, dx, dy, g, bx, by, junction, cost):
        out.append((cost, f"{d}{ln}:{bx},{by}", tuple((dx * i, dy * i) for i in range(1, ln + 1)), g, bx, by,
                    junction))

    for ln in range(1, 5):
        n = ln + 1  # the first cell past the leader is on the box border
        for s in (0, 3, 6):
            add("NE", ln, 1, -1, "╱", n - s, -n - (BH - 1), None, ln + s * 0.15)
            add("SE", ln, 1, 1, "╲", n - s, n, None, ln + s * 0.15 + 0.05)
            add("NW", ln, -1, -1, "╲", -n - (BW - 1) + s, -n - (BH - 1), None, ln + s * 0.15 + 0.1)
            add("SW", ln, -1, 1, "╱", -n - (BW - 1) + s, n, None, ln + s * 0.15 + 0.15)
        for j in (1, 2):
            add("E", ln, 1, 0, "─", n, -j, (n, 0, "┤"), ln + 0.3 + (j - 1) * 0.1)
            add("W", ln, -1, 0, "─", -n - (BW - 1), -j, (-n, 0, "├"), ln + 0.35 + (j - 1) * 0.1)
        for k in (1, 4, 8, 12, 16):
            add("N", ln, 0, -1, "│", -k, -n - (BH - 1), (0, -n, "┬"), ln + 0.5 + abs(k - BW / 2) * 0.03)
            add("S", ln, 0, 1, "│", -k, n, (0, n, "┴"), ln + 0.55 + abs(k - BW / 2) * 0.03)
    return sorted(out)


SPOTS = _spots()
SPOT_BY_KEY = {s[1]: s for s in SPOTS}


def place_tags(items, occ, soft, mw, mh, prev: dict, relaxed: bool):
    """Give each (plane, col, row) a tag spot that overlaps nothing in occ (bytearray rows, map cells).
    soft marks map labels: allowed but avoided. relaxed: if nothing is free, take any spot on the map."""
    placed, failed = [], 0

    def fit(s, pc, pr):
        cost, _, lead, _, bx, by, _ = s
        x0, y0 = pc + bx, pr + by
        if x0 < 0 or y0 < 0 or x0 + BW > mw or y0 + BH > mh:
            return None
        for dx, dy in lead:
            x, y = pc + dx, pr + dy
            if occ[y][x]:
                return None
            cost += 3 * soft[y][x]  # a leader through text is ugly
        for y in range(y0, y0 + BH):
            if any(occ[y][x0:x0 + BW]):
                return None
            cost += 0.4 * sum(soft[y][x0:x0 + BW])
        return cost

    for p, pc, pr in items:
        got = None
        if prev.get(p.hex) in SPOT_BY_KEY and fit(SPOT_BY_KEY[prev[p.hex]], pc, pr) is not None:
            got = SPOT_BY_KEY[prev[p.hex]]  # keep last frame's spot while it's free
        else:
            best = math.inf
            for s in SPOTS:
                if s[0] >= best:
                    break
                c = fit(s, pc, pr)
                if c is not None and c < best:
                    best, got = c, s
        if got is None and relaxed:
            got = next((s for s in SPOTS if 0 <= pc + s[4] and pc + s[4] + BW <= mw and 0 <= pr + s[5] and
                        pr + s[5] + BH <= mh), None)
        if got is None:
            failed += 1
            continue
        _, key, lead, g, bx, by, junction = got
        x0, y0 = pc + bx, pr + by
        for y in range(y0, y0 + BH):  # reserve the box with a one-column gutter, and the leader
            for x in range(max(0, x0 - 1), min(mw, x0 + BW + 1)):
                occ[y][x] = 2
        for dx, dy in lead:
            occ[pr + dy][pc + dx] = 2
        placed.append((p, pc, pr, got))
    return placed, failed


# --- the panel ---------------------------------------------------------------------------------------------------

class MapPanel(Panel):
    """The big map: line art centred on home at a chosen range, with two layers you switch on and off.
    Radar (`w`) is the RainViewer loop, animated. Air traffic (`a`) is live aircraft, plus the traffic list beside
    the map when the panel is wide enough. With air traffic off, each radar frame is drawn once and cached."""
    SIDE_W = 44        # traffic list + selected aircraft, shown with air traffic on when the panel is this wide
    SIDE_MIN = 120

    def __init__(self, **kw):
        super().__init__(**kw)
        self._bases: dict[tuple, tuple] = {}    # still map per radar frame: key -> (canvas, soft, any_rain)
        self._bases_for: tuple | None = None
        self._still: dict[tuple, list] = {}     # finished strips per radar frame while air traffic is off
        self._still_for: tuple | None = None
        self.hot: list[tuple[int, int, int, int, str]] = []   # click targets (x0, y0, x1, y1, hex) in panel cells
        self.order: list[str] = []                            # aircraft in traffic list order, for Tab
        self.map_rect = (0, 0, 0, 0)
        self._vis: list = []

    # geometry, also used by the app to size requests
    def map_cells(self) -> tuple[int, int, int]:
        w, h = self.size.width, self.size.height
        sw = self.SIDE_W if getattr(self.app, "air_on", False) and w >= self.SIDE_MIN else 0
        return max(2, w - 2 - sw), max(2, h - 2), sw

    def view(self) -> tuple[int, int, float]:
        mw, mh, _ = self.map_cells()
        rng = getattr(self.app, "air_range", DEFAULT_RANGE)
        return mw * 2, mh * 4, min(mw * 2, mh * 4) / 2 / rng

    def bbox(self, home) -> tuple[float, float, float, float]:
        """(lon0, lon1, lat0, lat1) of the map, the box /api/radar samples the rain for."""
        mw2, mh4, ppn = self.view()
        dlon = mw2 / 2 / ppn / (60 * math.cos(math.radians(home[0])))
        dlat = mh4 / 2 / ppn / 60
        return home[1] - dlon, home[1] + dlon, home[0] - dlat, home[0] + dlat

    def fetch_dist(self) -> int:
        """Radius in NM that reaches the map's corners."""
        mw2, mh4, ppn = self.view()
        return min(250, math.ceil(math.hypot(mw2, mh4) / 2 / ppn + 2))

    def on_resize(self, _event):
        self.app.request_radar()  # type: ignore[attr-defined]

    def on_click(self, event):
        if not getattr(self.app, "air_on", False):
            return
        for x0, y0, x1, y1, hx in self.hot:
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self.app.air_select(hx)  # type: ignore[attr-defined]
                return
        x, y, mw, mh = self.map_rect
        if x <= event.x < x + mw and y <= event.y < y + mh:
            self.app.air_select(None)  # type: ignore[attr-defined]

    def _radar(self):
        """The radar loop if the layer is on and the data was sampled for this map, else None."""
        app = self.app
        r = getattr(app, "radar", None) if getattr(app, "radar_on", True) else None
        mw, mh, _ = self.map_cells()
        if not r or (r.get("w"), r.get("h")) != (mw * 2, mh * 4) or not r.get("frames"):
            return None
        return r

    def build_strips(self) -> list:
        """With air traffic off the picture only changes with the radar frame, so each frame's strips are kept."""
        app = self.app
        r = self._radar()
        if getattr(app, "air_on", False) or r is None or self._space_near():
            return super().build_strips()
        frames = r["frames"]
        idx = min(getattr(app, "radar_idx", 0), len(frames) - 1)
        whole = (self.size.width, self.size.height, getattr(app, "air_range", DEFAULT_RANGE), self.pal.name,
                 id(getattr(app, "geo", None)), id(r), getattr(app, "loc", ""), self.label("name"))
        if whole != self._still_for:
            self._still_for, self._still = whole, {}
        key = (frames[idx]["t"], idx, bool(getattr(app, "radar_paused", False)))
        hit = self._still.get(key)
        if hit is None or len(hit) != self.size.height:
            hit = self._still[key] = super().build_strips()
        return hit

    def _space_near(self) -> bool:
        app = self.app
        return bool(getattr(app, "space_on", False) and getattr(getattr(app, "sky", None), "cand", None))

    # --- drawing ---
    def draw(self, c, t, w, h):
        app = self.app
        tr: Traffic = app.traffic  # type: ignore[attr-defined]
        geo = getattr(app, "geo", None)
        rng = getattr(app, "air_range", DEFAULT_RANGE)
        air_on = getattr(app, "air_on", False)
        wx_on = getattr(app, "radar_on", True)
        mode = getattr(app, "air_tags", "AUTO")
        now = time.time()
        mw, mh, sw = self.map_cells()
        MW, MH, ppn = mw * 2, mh * 4, min(mw * 2, mh * 4) / 2 / rng
        self.map_rect = (1, 1, mw, mh)
        self.hot = []
        home = tuple(geo["home"]) if geo else tr.home
        radar = self._radar()
        frames = (radar or {}).get("frames") or []
        idx = min(getattr(app, "radar_idx", 0), len(frames) - 1) if frames else -1
        fr = frames[idx] if frames else None
        planes = list(tr.planes.values()) if air_on else []
        airborne = sum(1 for p in planes if not p.ground)

        layers = []
        if wx_on:
            layers.append(f"RADAR · {((getattr(app, 'radar', None) or {}).get('source') or '--').upper()}")
        if air_on:
            layers.append(f"AIR TRAFFIC · {tr.source or 'ADS-B'}")
        space_on = getattr(app, "space_on", False)
        if space_on:
            layers.append("SPACE")
        title = "  ".join(layers) or "MAP"
        c.box(0, 0, w, h, t.dim, title, t.bright)
        sep = 1 + mw
        stats = []   # most important first; the rest drop off when the border is too short
        if air_on and tr.ok_at:
            stats.append(f"{airborne} AIRBORNE · {len(planes) - airborne} GROUND")
        if air_on and mode == "AUTO" and not tr.tags_full and any(not p.ground for p in planes):
            stats.append("TAGS BUSY")   # too many aircraft for a tag each at this range: Tab steps through them
        if space_on:
            stats.append("")   # filled in once the spacecraft on the map are known (below)
        if fr:   # the timeline in the bottom border shows the frame too, so this goes first when space is short
            state = "PAUSED" if getattr(app, "radar_paused", False) else "LOOP"
            stats.append(f"{hm(datetime.fromtimestamp(fr['t'], panels.TZ))} {idx + 1}/{len(frames)} {state}")
        self._stats = (stats, title, sep, mw)   # put in the border after the spacecraft are drawn
        if sw:
            c.put(sep, 0, "┬", t.dim)
            c.put(sep, h - 1, "┴", t.dim)
            for y in range(1, h - 1):
                c.put(sep, y, "│", t.dim)
        keys = (f" [A] AIR {'ON' if air_on else 'OFF'}  [W] RADAR {'ON' if wx_on else 'OFF'}  "
                f"[O] SPACE {'ON' if space_on else 'OFF'}  [+/-] {rng} NM "
                + (f" [T] TAGS {mode}  [TAB] SELECT " if air_on else ""))
        timeline = ""
        if frames:
            tl = "".join("█" if i == idx else "▪" for i in range(len(frames)))
            first, last = (hm(datetime.fromtimestamp(frames[i]["t"], panels.TZ)) for i in (0, -1))
            timeline = f" {first} {tl} {last} "
        if timeline and len(keys) + len(timeline) + 4 <= mw:
            c.put(sep - len(timeline) - 1, h - 1, timeline, t.fg)
        c.put(2, h - 1, keys[: max(0, mw - 2)], t.dim)
        if not home:
            c.put(3, 2, "LOADING MAP…", t.dim)
            self._border_stats(c, t, "")
            return

        lat0, lon0 = home
        kx, ky = 60 * math.cos(math.radians(lat0)) * ppn, 60 * ppn

        def P(lat, lon):
            return MW / 2 + (lon - lon0) * kx, MH / 2 - (lat - lat0) * ky

        mark = "HOME" if getattr(app, "loc", "") == "home" else self.label("name", "HERE")
        dens = WX_DENS if air_on else DENS
        whole = (MW, MH, rng, t.name, id(geo), home, mark, dens)
        if whole != self._bases_for:
            self._bases_for, self._bases = whole, {}
        key = fr["t"] if fr else None
        if key not in self._bases:
            self._bases[key] = self._build_base(t, mw, mh, P, ppn, rng, geo, fr, dens, not air_on, mark)
        base, soft, any_rain = self._bases[key]
        for y in range(mh):  # paste the map
            bt, bc, bs, bd, bds = base.text[y], base.chars[y], base.cstyle[y], base.dots[y], base.dstyle[y]
            for x in range(mw):
                if bt[x]:
                    c.put(1 + x, 1 + y, bc[x], bs[x])
                elif bd[x]:
                    c.dots[1 + y][1 + x], c.dstyle[1 + y][1 + x] = bd[x], bds[x]

        if space_on and app.sky.cand:  # type: ignore[attr-defined]
            soft = [row[:] for row in soft]   # the base's copy stays clean for the next frame
        over = self._spacecraft(c, t, mw, mh, P, now, soft) if space_on else []
        if space_on:
            stations = [s for s in over if s[0].station]
            nxt = getattr(app, "space_next", None)
            if stations:
                note = f"{stations[0][0].name} OVER THE MAP"
            elif nxt and nxt[1] > now:
                note = f"{nxt[0]} IN {fmt_until(nxt[1], now)}"
            else:
                note = f"{len(over)} SATELLITE{'S' if len(over) != 1 else ''}" if over else ""
            self._border_stats(c, t, note)
        else:
            self._border_stats(c, t, "")
        if air_on:
            self._traffic(c, t, tr, planes, mw, mh, P, mode, soft, now, lat0, lon0)
        if wx_on:
            self._legend(c, t, mw, mh, fr, any_rain)

        # beside the map: selected aircraft, then the traffic list
        self.order = []
        if not air_on:
            return
        vis = self._vis
        if sw:
            self._side(c, t, tr, sep, w, h, vis, now, home)
        else:
            self.order = [v[0].hex for v in sorted(vis, key=lambda v: (v[0].ground, v[0].d_home))]

    def _border_stats(self, c, t, space_note: str):
        """Counts in the top border, most important first; the rest drop off when it's too short."""
        stats, title, sep, mw = self._stats
        stats = [space_note if s == "" else s for s in stats]
        stats = [s for s in stats if s]
        while stats and len(" · ".join(stats)) + len(title) + 10 >= mw:
            stats.pop()
        if stats:
            right = f" {' · '.join(stats)} "
            c.put(sep - len(right) - 2, 0, right, t.bright)

    def _spacecraft(self, c, t, mw, mh, P, now, soft=None) -> list:
        """Spacecraft on the map now: a dotted ground track 90 s behind and ahead, a marker (◆ crewed station,
        ◇ anything else) and the name and altitude. Drawn under the aircraft, whose data tags steer around the label
        (it's marked in `soft`, like a town name). Returns what's on the map."""
        app = self.app
        sky = app.sky  # type: ignore[attr-defined]
        if not sky.cand:
            return []
        lon0, lon1, lat0, lat1 = self.bbox(app.geo["home"])  # type: ignore[attr-defined]
        try:
            over = sky.now(sky.cand, now, (lon0, lon1, lat0, lat1))
        except IndexError:   # the elements were just reloaded; the next scan picks the new indices
            return []
        MW, MH = mw * 2, mh * 4
        for craft, lat, lon, alt, track in over:
            st = t.accent if craft.station else t.dim
            n = [0]

            def dot(x, y, s, prio, n=n):   # every other dot: a dotted line
                n[0] += 1
                if n[0] % 2 == 0 and 0 <= x < MW and 0 <= y < MH:
                    c.dot(2 + x, 4 + y, s, prio)

            pts = [P(a, b) for a, b in track]
            for (x0, y0), (x1, y1), (_, la), (_, lb) in zip(pts, pts[1:], track, track[1:]):
                if abs(lb - la) < 180:
                    self._seg(dot, (x0, y0), (x1, y1), st, 3)
            x, y = P(lat, lon)
            pc, pr = int(x) // 2, int(y) // 4
            if not (0 <= pc < mw and 0 <= pr < mh):
                continue
            c.put(1 + pc, 1 + pr, "◆" if craft.station else "◇", t.accent if craft.station else t.fg)
            # the label goes right of the marker, else left, wherever it covers no town name or other label;
            # failing that, just the name; failing that, a station's label still goes on (the headline), others
            # keep only their marker
            lx = None
            for lab in (f"{craft.name} {alt:,.0f} KM", craft.name):
                for x0 in (pc + 2, pc - len(lab) - 1):
                    if 0 <= x0 and x0 + len(lab) <= mw and self._clear(soft, pr, x0 - 1, x0 + len(lab) + 1, mw):
                        lx = x0
                        break
                if lx is not None:
                    break
            if lx is None and craft.station:
                lab = f"{craft.name} {alt:,.0f} KM"
                lx = pc + 2 if pc + 2 + len(lab) <= mw else pc - len(lab) - 1
            if lx is not None and lx >= 0:
                c.put(1 + lx, 1 + pr, lab, t.accent if craft.station else t.fg)
                if soft is not None:
                    for i in range(min(lx, pc), max(lx + len(lab), pc + 1)):
                        soft[pr][i] = 1
            elif soft is not None:
                soft[pr][pc] = 1
        return over

    @staticmethod
    def _clear(soft, row, a, b, mw) -> bool:
        """No town name or label in columns a..b-1 of a map row (soft is None in tests: everything's clear)."""
        if soft is None:
            return True
        r = soft[row]
        return not any(r[i] for i in range(max(a, 0), min(b, mw, len(r))))

    @staticmethod
    def _legend(c, t, mw, mh, fr, any_rain):
        """dBZ scale in the map's bottom-right corner, and a note when there's no rain (or no radar yet)."""
        y = mh   # last map row, in panel cells
        lx = 1 + mw - 30
        if lx > 30:
            c.put(lx - 1, y, " " * 31, t.dim)
            c.put(lx, y, "DBZ 10", t.dim)
            for i, col in enumerate(t.levels):
                c.put(lx + 7 + i * 4, y, "██", col)
            c.put(lx + 27, y, "50", t.dim)
        note = "LOADING RADAR…" if fr is None else "" if any_rain else "NO PRECIPITATION IN RANGE"
        if note:
            c.put(2, y, note, t.dim)

    def _traffic(self, c, t, tr, planes, mw, mh, P, mode, soft, now, lat0, lon0):
        """Trails, aircraft and data tags over the map."""
        MW, MH = mw * 2, mh * 4

        def mdot(x, y, st, prio):
            if 0 <= x < MW and 0 <= y < MH:
                c.dot(2 + x, 4 + y, st, prio)

        # aircraft on the map this frame
        vis = []
        for p in planes:
            p.now = p.pos(now)
            p.d_home = dist_nm(lat0, lon0, *p.now)
            x, y = P(*p.now)
            p.xy = (x, y)
            pc, pr = int(x) // 2, int(y) // 4
            if 0 <= x < MW and 0 <= y < MH:
                vis.append((p, pc, pr))
        self._vis = vis
        sel = tr.selected

        # trails: the last 90 s, or the whole history for the selected aircraft
        for p, _, _ in vis:
            is_sel = p.hex == sel
            prev = p.xy
            for lat, lon, ts, _a in reversed(p.trail):
                if not is_sel and now - ts > 90:
                    break
                cur = P(lat, lon)
                if abs(cur[0] - prev[0]) + abs(cur[1] - prev[1]) >= 1:
                    self._seg(mdot, prev, cur, t.accent if is_sel else t.dim, 6 if is_sel else 2)
                    prev = cur

        # aircraft: parked first, then low to high, selected on top. They draw over map labels on purpose: where a
        # plane is matters more than a label it covers for a moment.
        occ = [bytearray(mw) for _ in range(mh)]
        vis.sort(key=lambda v: (v[0].hex == sel, v[0].emergency, not v[0].ground, v[0].alt or 0))
        blink = int(now) % 2 == 0
        for p, pc, pr in vis:
            occ[pr][pc] = 1
            if p.hex == sel:
                st = f"{t.bg} on {t.accent}"
                for k in range(24):  # a ring around the selected aircraft
                    a = 2 * math.pi * k / 24
                    mdot(int(p.xy[0] + 7 * math.cos(a)), int(p.xy[1] + 7 * math.sin(a)), t.accent, 9)
            elif p.emergency:
                st = t.warn if blink else t.bright
            else:
                st = t.dim if p.ground else t.bright
            c.put(1 + pc, 1 + pr, glyph(p), st)
            self.hot.append((pc, 1 + pr, pc + 2, 1 + pr, p.hex))

        # data tags
        special = [v for v in vis if v[0].hex == sel]
        auto = [v for v in vis if v[0].hex != sel and not v[0].ground]
        for v in auto:  # crowded aircraft choose first, while there is still room around them
            v[0].crowd = sum(1 for o in vis if abs(o[1] - v[1]) <= BW + 2 and abs(o[2] - v[2]) <= BH + 2)
        auto.sort(key=lambda v: (-v[0].crowd, v[0].d_home))
        order = special + ([] if mode == "OFF" else auto)
        if mode == "AUTO":
            too_many = len(auto) * (BW + 1) * (BH + 1) > mw * mh * 0.55
            result = None
            fits = False
            if not too_many:
                # tags show when (nearly) every airborne aircraft gets one; a few left in a crowd go without.
                # Showing them tolerates more misses than bringing them back does, so they don't flap at the edge.
                trial = place_tags(order, [r[:] for r in occ], soft, mw, mh, tr.placed, False)
                fits = trial[1] <= len(auto) // (6 if tr.tags_full else 10)
                if tr.tags_full or fits:
                    result = trial
            if fits != tr.tags_full:  # wait 1.5 s before switching, so tags don't flicker at the threshold
                if not tr.tags_pending or tr.tags_pending[0] != fits:
                    tr.tags_pending = (fits, now)
                elif now - tr.tags_pending[1] > 1.5:
                    tr.tags_full, tr.tags_pending = fits, None
            else:
                tr.tags_pending = None
            if not tr.tags_full or result is None:
                result = place_tags(special, [r[:] for r in occ], soft, mw, mh, tr.placed, True)
        elif mode == "ALL":
            result = place_tags(order, [r[:] for r in occ], soft, mw, mh, tr.placed, False)
            if special and not any(pl[0].hex == sel for pl in result[0]):
                result[0].extend(place_tags(special, [r[:] for r in occ], soft, mw, mh, {}, True)[0])
        else:
            result = place_tags(special, [r[:] for r in occ], soft, mw, mh, tr.placed, True)
        placed = sorted(result[0], key=lambda pl: pl[0].hex == sel)
        tr.placed = {pl[0].hex: pl[3][1] for pl in placed}
        for pl in placed:
            self._tag(c, t, tr, *pl, sel=pl[0].hex == sel, blink=blink)

        if tr.error:
            c.put(2, 1, f" FEED: {tr.error} "[: mw - 2], t.warn)
        elif not tr.ok_at:
            c.put(3, 2, "WAITING FOR ADS-B…", t.dim)

    @staticmethod
    def _seg(mdot, a, b, st, prio):
        x0, y0, x1, y1 = int(round(a[0])), int(round(a[1])), int(round(b[0])), int(round(b[1]))
        n = max(abs(x1 - x0), abs(y1 - y0), 1)
        if n > 2000:
            return
        for i in range(n + 1):
            mdot(x0 + (x1 - x0) * i // n, y0 + (y1 - y0) * i // n, st, prio)

    def _build_base(self, t, mw, mh, P, ppn, rng, geo, wx_frame, dens, rain_on_top, mark):
        """The still part of the map for one radar frame: rain, line art, runways, rings and labels.
        Returns (canvas, soft label cells, any rain). Kept per frame until the size, range, theme or place changes."""
        b = Canvas(mw, mh)
        MW, MH = mw * 2, mh * 4
        soft = [bytearray(mw) for _ in range(mh)]

        any_rain = False
        if wx_frame:  # with air traffic on the rain is sparser and sits under the coast and roads
            for y, row in enumerate(wx_frame["rows"][:MH]):
                if row.count("0") == len(row):
                    continue
                for x, ch in enumerate(row[:MW]):
                    if ch == "0":
                        continue
                    lv = int(ch) if ch.isdigit() else "abcdef".index(ch) + 1
                    if BAYER[y & 3][x & 1] < dens[min(lv, 5)]:
                        b.dot(x, y, t.snow if ch.isalpha() else t.levels[min(lv, 5) - 1], 5 + lv if rain_on_top else 0)
                        any_rain = True

        def poly(ln, st, prio, dash=0):
            pts = [P(lat, lon) for lon, lat in ln]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                if (x0 < 0 and x1 < 0) or (y0 < 0 and y1 < 0) or (x0 >= MW and x1 >= MW) or (y0 >= MH and y1 >= MH):
                    continue  # off the map
                b.line(x0, y0, x1, y1, st, prio, dash)

        geo = geo or {}
        for ln in geo.get("roads", []):
            poly(ln, t.c("faint"), 1)
        for ln in geo.get("lakes", []):
            poly(ln, t.c("land"), 3)
        for k in ("border", "state"):
            for ln in geo.get(k, []):
                poly(ln, t.dim, 2, dash=2)
        for ln in geo.get("coast", []):
            poly(ln, t.c("land"), 3)
        for (lon1, lat1), (lon2, lat2) in geo.get("runways", []):
            (x1, y1), (x2, y2) = P(lat1, lon1), P(lat2, lon2)
            b.line(x1, y1, x2, y2, t.fg, 4)
            if ppn > 14:  # close in, draw runways two dots wide
                ox, oy = (1, 0) if abs(y2 - y1) > abs(x2 - x1) else (0, 1)
                b.line(x1 + ox, y1 + oy, x2 + ox, y2 + oy, t.fg, 4)

        # range rings around home, out to the corners
        hx, hy = MW / 2, MH / 2
        step = ring_step(rng)
        r = step
        while r * ppn < math.hypot(MW, MH) / 2:
            rad = r * ppn
            n = max(8, int(2 * math.pi * rad / 3))
            for k in range(0, n, 2):
                a = 2 * math.pi * k / n
                b.dot(int(hx + rad * math.cos(a)), int(hy + rad * math.sin(a)), t.dim, 1)
            r += step

        def label(cx, cy, s, st) -> bool:
            """Put a label if it has a free cell on each side, so two labels never run together (NZYSAN)."""
            if not (0 <= cy < mh) or cx < 0 or cx + len(s) > mw or any(soft[cy][max(0, cx - 1):cx + len(s) + 1]):
                return False
            b.put(cx, cy, s, st)
            for i in range(len(s)):
                soft[cy][cx + i] = 1
            return True

        label(int(hx) // 2, int(hy) // 4, "⌂ " + mark, t.bright) or label(int(hx) // 2, int(hy) // 4, "⌂", t.bright)
        r = step
        while r * ppn < math.hypot(MW, MH) / 2:
            rad = r * ppn
            x, y = hx + rad * 0.71, hy - rad * 0.71
            if 0 <= x < MW and 0 <= y < MH:
                label(int(x) // 2 + 1, int(y) // 4, f"{r:g} NM", t.dim)
            r += step
        ends = [P(lat, lon) for ln in geo.get("runways", []) for lon, lat in ln]
        for code, _name, lat, lon, size in geo.get("airports", []):
            if size != "L" and rng > 40:
                continue
            x, y = P(lat, lon)
            if not (0 <= x < MW and 0 <= y < MH):
                continue
            near = [ey for ex, ey in ends if abs(ex - x) + abs(ey - y) < 3.5 * ppn]
            cy = int(max(near + [y])) // 4 + 1   # just below the airport's runways
            cx = int(x) // 2 - len(code) // 2
            label(cx, cy, code, t.accent) or label(cx, cy - 2 - int(y - min(near + [y])) // 4, code, t.accent)
        for name, lat, lon in geo.get("towns", []):
            x, y = P(lat, lon)
            cx, cy = int(x) // 2, int(y) // 4
            if 0 <= x < MW and 0 <= y < MH:
                label(cx, cy, "·" + name, t.dim) if cx < mw - len(name) - 2 else label(cx - len(name), cy, name + "·",
                                                                                         t.dim)
        return b, soft, any_rain

    def _tag(self, c, t, tr, p, pc, pr, spot, sel, blink):
        _, _, lead, g, bx, by, junction = spot
        ox, oy = 1 + pc, 1 + pr   # panel cell of the aircraft
        frame = t.accent if sel else t.dim
        for dx, dy in lead:
            c.put(ox + dx, oy + dy, g, frame)
        x0, y0 = ox + bx, oy + by
        tl, tr_, bl, br, hz, vt = "╔╗╚╝═║" if sel else "┌┐└┘─│"
        c.put(x0, y0, tl + hz * (BW - 2) + tr_, frame)
        for yy in range(y0 + 1, y0 + BH - 1):
            c.put(x0, yy, vt + " " * (BW - 2) + vt, frame)
        c.put(x0, y0 + BH - 1, bl + hz * (BW - 2) + br, frame)
        if junction and not sel:
            c.put(ox + junction[0], oy + junction[1], junction[2], frame)
        title = f" {p.callsign[:BW - 6]} "
        c.put(x0 + 1, y0, title, (t.warn if blink else t.bright) if p.emergency else t.accent if sel else t.bright)
        if p.emergency:
            c.put(x0 + 1 + len(title), y0, p.squawk or "EMRG", t.warn)
        extra = tr.route_text(p) or (p.reg if p.reg and p.reg != p.callsign else f"SQK {p.squawk}" if p.squawk else "")
        text = t.accent if sel else t.fg
        c.put(x0 + 2, y0 + 1, f"{p.type or '----':<4} {extra}"[:IW], text)
        c.put(x0 + 2, y0 + 2, f"{alt_text(p)}{vr_glyph(p)} {spd(p)}KT"[:IW], text)
        self.hot.append((x0, y0, x0 + BW - 1, y0 + BH - 1, p.hex))

    def _side(self, c, t, tr, sep, w, h, vis, now, home):
        x, iw = sep + 2, w - sep - 4
        y = 0

        def header(yy, title, right=""):
            if yy:
                c.put(sep, yy, "├" + "─" * (w - sep - 2) + "┤", t.dim)
            c.put(sep + 2, yy, f" {title} ", t.bright)
            if right:
                c.put(w - len(right) - 4, yy, f" {right} ", t.dim)

        p = tr.planes.get(tr.selected) if tr.selected else None
        if p is not None and h >= 30:
            header(y, "SELECTED", "[ESC]")
            lines = self._detail(t, tr, p, home)
            for i, (s, st) in enumerate(lines):
                c.put(x, y + 1 + i, s[:iw], st)
            gy = y + 2 + len(lines)
            self._profile(c, t, p, x, gy, iw, now)
            y = gy + 3

        lst = sorted((v[0] for v in vis), key=lambda q: (q.ground, q.d_home))
        self.order = [q.hex for q in lst]
        header(y, "TRAFFIC", f"{len(lst)} IN VIEW")
        c.put(x, y + 1, "FLIGHT   TYPE   ALT   KT   NM ROUTE"[:iw], t.dim)
        rows = h - 2 - (y + 2)
        start = 0
        if tr.selected in self.order and rows > 0:
            i = self.order.index(tr.selected)
            start = max(0, i - rows + 1)
        for k, q in enumerate(lst[start:start + max(0, rows)]):
            yy = y + 2 + k
            if q.hex == tr.selected:
                st = f"{t.bg} on {t.accent}"
            else:
                st = t.warn if q.emergency else t.dim if q.ground else t.fg
            nm = f"{q.d_home:.1f}" if q.d_home < 100 else f"{q.d_home:.0f}"
            line = (f"{q.callsign[:8]:<8} {q.type[:4]:<4} {alt_text(q, True):>5}{vr_glyph(q)} {spd(q):>3} {nm:>4} "
                    f"{tr.route_text(q)}")
            c.put(x - 1, yy, f" {line:<{iw}} "[: iw + 2], st)
            self.hot.append((sep + 1, yy, w - 2, yy, q.hex))
        if len(lst) > rows > 0:
            more = f"{start + 1}-{min(len(lst), start + rows)}/{len(lst)}"
            c.put(w - len(more) - 4, h - 1, f" {more} ", t.dim)

    @staticmethod
    def _detail(t, tr: Traffic, p: Plane, home) -> list[tuple[str, str]]:
        out = [(f"{p.callsign}  {p.reg if p.reg != p.callsign else ''}  {p.type}".replace("    ", "  "),
                t.warn if p.emergency else t.accent)]
        if p.emergency:
            out.append((f"EMERGENCY · SQUAWK {p.squawk or '----'}", t.warn))
        if p.desc:
            out.append((p.desc, t.fg))
        r = tr.routes.get(p.flight)
        airline = (r.get("airline") or "").upper() if isinstance(r, dict) else ""
        if airline or p.op:  # the airline flying it reads better than the registered owner (often a lessor)
            out.append((airline or p.op, t.fg))
        ok = tr.route(p)
        if ok:
            for pre, a in (("", ok["origin"]), (" › ", ok["dest"])):
                out.append((f"{pre}{_ap(a)} {(a.get('city') or a.get('name') or '').upper()}", t.bright))
        elif isinstance(r, dict):
            out.append(("ROUTE ON FILE DOESN'T FIT, HIDDEN", t.dim))
        elif p.flight and AIRLINE.match(p.flight):
            out.append(("ROUTE …" if r == "pending" or p.flight not in tr.routes else "ROUTE UNKNOWN", t.dim))
        vr = "" if p.vr is None or p.ground else f"  {vr_glyph(p).strip() or '='} {abs(round(p.vr / 64) * 64)} FPM"
        out.append((f"ALT {'GROUND' if p.ground else '---' if p.alt is None else f'{p.alt:,.0f} FT'}{vr}", t.fg))
        trk = "---" if p.track is None else f"{round(p.track) % 360:03d}°"
        out.append((f"GS {spd(p)} KT  TRK {trk}" + (f"  SEL {p.nav_alt:,.0f}" if p.nav_alt else ""), t.fg))
        brg = f"{round(bearing(home[0], home[1], *p.now)) % 360:03d}°"
        out.append((f"{p.d_home:.1f} NM @ {brg} FROM HOME  SQK {p.squawk or '----'}", t.fg))
        out.append((f"HEX {p.hex.upper()}  {p.src}  SEEN {p.seen if p.seen is not None else '-'}S", t.dim))
        return out

    @staticmethod
    def _profile(c, t, p: Plane, x, y, iw, now):
        """Altitude over the trail's time span, as a two-row braille line."""
        pts = [(ts, a) for _, _, ts, a in p.trail if a is not None]
        if len(pts) < 2:
            return
        top = max(1000, max(a for _, a in pts))
        t0, t1 = pts[0][0], max(now, pts[0][0] + 1)
        c.put(x, y, f"FL{top / 100:.0f}" if top >= 18000 else f"{top:,.0f}", t.dim)
        c.put(x, y + 1, f"{(t1 - t0) / 60:.0f}M", t.dim)
        gx, gw = x + 7, iw - 7
        X = lambda ts: gx * 2 + (ts - t0) / (t1 - t0) * (gw * 2 - 1)  # noqa: E731
        Y = lambda a: y * 4 + 7 - a / top * 7  # noqa: E731
        c.polyline([(X(ts), Y(a)) for ts, a in pts], t.accent, 6)
