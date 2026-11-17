"""AIR map: live air traffic around home, drawn like the radar map. Ported from AsciiFlights.

The map is the radar map's line art (coast, borders, highways, towns) plus runways and airport codes, centred on home
at a range you choose, with the latest radar frame dithered underneath. Each aircraft is an arrow pointing along its
track with a short braille trail, and can carry a data tag (callsign, type, route or registration, altitude,
climb/descent, ground speed) on a leader line. Between polls, positions are dead-reckoned so traffic moves smoothly.
When the panel is wide enough, a traffic list and the selected aircraft's details sit beside the map.

Data comes from the collector: /api/aircraft (adsb.fi, adsb.lol) and /api/routes (adsbdb).
"""
from __future__ import annotations

import math
import re
import time
from collections import deque

from .canvas import BAYER, Canvas
from .panels import Panel

RANGES = (5, 10, 15, 25, 40, 60, 100)   # NM from home to the nearest map edge; map lines reach about 100 NM
TAG_MODES = ("AUTO", "ALL", "OFF")
ARROWS = "↑↗→↘↓↙←↖"
R_NM = 3440.065
WX_DENS = (0, 1, 2, 3, 4, 5)            # radar under the traffic is sparser than on the radar map
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
    """Aircraft, routes and feed status for the AIR map. Lives on the app; the panel only reads it."""

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

class AirPanel(Panel):
    SIDE_W = 44        # traffic list + selected aircraft, shown when the panel is at least SIDE_MIN wide
    SIDE_MIN = 120

    def __init__(self, **kw):
        super().__init__(**kw)
        self._base: tuple | None = None
        self.hot: list[tuple[int, int, int, int, str]] = []   # click targets (x0, y0, x1, y1, hex) in panel cells
        self.order: list[str] = []                            # aircraft in traffic list order, for Tab
        self.map_rect = (0, 0, 0, 0)

    # geometry, also used by the app to size requests
    def map_cells(self) -> tuple[int, int, int]:
        w, h = self.size.width, self.size.height
        sw = self.SIDE_W if w >= self.SIDE_MIN else 0
        return max(2, w - 2 - sw), max(2, h - 2), sw

    def view(self) -> tuple[int, int, float]:
        mw, mh, _ = self.map_cells()
        rng = getattr(self.app, "air_range", 25)
        return mw * 2, mh * 4, min(mw * 2, mh * 4) / 2 / rng

    def bbox(self, home) -> tuple[float, float, float, float]:
        """(lon0, lon1, lat0, lat1) of the map, the box /api/radar samples for the weather underneath."""
        mw2, mh4, ppn = self.view()
        dlon = mw2 / 2 / ppn / (60 * math.cos(math.radians(home[0])))
        dlat = mh4 / 2 / ppn / 60
        return home[1] - dlon, home[1] + dlon, home[0] - dlat, home[0] + dlat

    def fetch_dist(self) -> int:
        """Radius in NM that reaches the map's corners."""
        mw2, mh4, ppn = self.view()
        return min(250, math.ceil(math.hypot(mw2, mh4) / 2 / ppn + 2))

    def on_resize(self, _event):
        self.app.request_air_wx()  # type: ignore[attr-defined]

    def on_click(self, event):
        for x0, y0, x1, y1, hx in self.hot:
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self.app.air_select(hx)  # type: ignore[attr-defined]
                return
        x, y, mw, mh = self.map_rect
        if x <= event.x < x + mw and y <= event.y < y + mh:
            self.app.air_select(None)  # type: ignore[attr-defined]

    # --- drawing ---
    def draw(self, c, t, w, h):
        app = self.app
        tr: Traffic = app.traffic  # type: ignore[attr-defined]
        geo = getattr(app, "geo", None)
        rng = getattr(app, "air_range", 25)
        mode = getattr(app, "air_tags", "AUTO")
        now = time.time()
        mw, mh, sw = self.map_cells()
        MW, MH, ppn = mw * 2, mh * 4, min(mw * 2, mh * 4) / 2 / rng
        self.map_rect = (1, 1, mw, mh)
        self.hot = []
        home = tuple(geo["home"]) if geo else tr.home
        planes = list(tr.planes.values())
        air = sum(1 for p in planes if not p.ground)
        c.box(0, 0, w, h, t.dim, f"AIR TRAFFIC · {tr.source or 'ADS-B'}", t.bright)
        sep = 1 + mw
        stats = f" {rng} NM · {air} AIRBORNE · {len(planes) - air} GROUND " if tr.ok_at else ""
        if stats and len(stats) + 30 < mw:
            c.put(sep - len(stats) - 2, 0, stats, t.bright)
        if sw:
            c.put(sep, 0, "┬", t.dim)
            c.put(sep, h - 1, "┴", t.dim)
            for y in range(1, h - 1):
                c.put(sep, y, "│", t.dim)
        wx_on = getattr(app, "air_wx", True)
        keys = (f" [A] WX MAP  [+/-] RANGE {rng} NM  [TAB] SELECT  [T] TAGS {mode}  "
                f"[W] WX {'ON' if wx_on else 'OFF'} ")
        c.put(2, h - 1, keys[: max(0, mw - 2)], t.dim)
        if not home:
            c.put(3, 2, "LOADING MAP…", t.dim)
            return

        lat0, lon0 = home
        kx, ky = 60 * math.cos(math.radians(lat0)) * ppn, 60 * ppn

        def P(lat, lon):
            return MW / 2 + (lon - lon0) * kx, MH / 2 - (lat - lat0) * ky

        wx = getattr(app, "air_wxgrid", None) if wx_on else None
        if wx and (wx.get("w"), wx.get("h")) != (MW, MH):
            wx = None
        frame = (wx or {}).get("frames") or []
        key = (MW, MH, rng, t.name, id(geo), frame[-1]["t"] if frame else None, home)
        if not self._base or self._base[0] != key:
            self._base = (key, *self._build_base(t, mw, mh, P, ppn, rng, geo, frame[-1] if frame else None))
        _, base, soft = self._base
        for y in range(mh):  # paste the map
            bt, bc, bs, bd, bds = base.text[y], base.chars[y], base.cstyle[y], base.dots[y], base.dstyle[y]
            for x in range(mw):
                if bt[x]:
                    c.put(1 + x, 1 + y, bc[x], bs[x])
                elif bd[x]:
                    c.dots[1 + y][1 + x], c.dstyle[1 + y][1 + x] = bd[x], bds[x]

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

        # aircraft: parked first, then low to high, selected on top
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
        if mode == "AUTO" and not tr.tags_full and auto:
            msg = " TOO BUSY FOR TAGS · [TAB] STEPS THROUGH · [T] SHOWS MORE "
            c.put(1 + max(0, (mw - len(msg)) // 2), 1, msg[:mw], f"{t.bg} on {t.dim}")

        if tr.error:
            c.put(2, 2 if mode == "AUTO" and not tr.tags_full else 1, f" FEED: {tr.error} "[: mw - 2], t.warn)
        elif not tr.ok_at:
            c.put(3, 2, "WAITING FOR ADS-B…", t.dim)

        # beside the map: selected aircraft, then the traffic list
        self.order = []
        if sw:
            self._side(c, t, tr, sep, w, h, vis, now, home)
        else:
            self.order = [v[0].hex for v in sorted(vis, key=lambda v: (v[0].ground, v[0].d_home))]

    @staticmethod
    def _seg(mdot, a, b, st, prio):
        x0, y0, x1, y1 = int(round(a[0])), int(round(a[1])), int(round(b[0])), int(round(b[1]))
        n = max(abs(x1 - x0), abs(y1 - y0), 1)
        if n > 2000:
            return
        for i in range(n + 1):
            mdot(x0 + (x1 - x0) * i // n, y0 + (y1 - y0) * i // n, st, prio)

    def _build_base(self, t, mw, mh, P, ppn, rng, geo, wx_frame):
        """The still part of the map: radar, line art, runways, rings and labels. Rebuilt only when it changes."""
        b = Canvas(mw, mh)
        MW, MH = mw * 2, mh * 4
        soft = [bytearray(mw) for _ in range(mh)]

        if wx_frame:  # latest radar frame, sparse, under everything
            for y, row in enumerate(wx_frame["rows"][:MH]):
                if row.count("0") == len(row):
                    continue
                for x, ch in enumerate(row[:MW]):
                    if ch == "0":
                        continue
                    lv = int(ch) if ch.isdigit() else "abcdef".index(ch) + 1
                    if BAYER[y & 3][x & 1] < WX_DENS[min(lv, 5)]:
                        b.dot(x, y, t.snow if ch.isalpha() else t.levels[min(lv, 5) - 1], 0)

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
            if not (0 <= cy < mh) or cx < 0 or cx + len(s) > mw or any(soft[cy][cx:cx + len(s)]):
                return False
            b.put(cx, cy, s, st)
            for i in range(len(s)):
                soft[cy][cx + i] = 1
            return True

        b.put(int(hx) // 2, int(hy) // 4, "⌂", t.bright)
        soft[int(hy) // 4][int(hx) // 2] = 1
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
        return b, soft

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
