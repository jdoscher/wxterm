"""APRS: stations heard on APRS-IS around home, drawn on the weather map.

`x` cycles the APRS view: OFF, WX (APRS weather stations' temperatures over the radar loop, like a surface map),
ALL (every station over the radar loop) and MAP (the big map becomes an APRS map: centred on home at a range you
choose with [ and ], callsigns, tracks, the latest radar frame underneath and a heard list beside it).

Data comes from the collector's /api/aprs (collector/wxcollect/aprs.py). Two rotating extras slots, APRS HEARD and
APRS WEATHER, read the same data.
"""
from __future__ import annotations

import math
import time
from datetime import datetime

from . import panels
from .canvas import BAYER, Canvas
from .glyphs import compass_name
from .panels import Panel, fmt, hm

VIEWS = ("OFF", "WX", "ALL", "MAP")
RANGES = (5, 10, 25, 50, 75, 100)        # miles from home to the nearest map edge (the feed reaches ~93 mi)
ARROWS = "↑↗→↘↓↙←↖"
TRAIL_MIN = 30                           # minutes of track drawn behind a moving station
STALE_MIN = 30                           # stations not heard for this long are drawn dim

# APRS symbol code (primary table) -> (glyph, kind). The alternate table mostly reuses the primary meanings.
_MOBILE = ">=<FRUabefjkuv*pS"
SYMBOLS = {c: (None, "MOBILE") for c in _MOBILE} | {
    "'": ("^", "AIR"), "^": ("^", "AIR"), "X": ("^", "HELO"), "g": ("^", "GLIDER"), "O": ("o", "BALLOON"),
    "s": ("~", "BOAT"), "Y": ("~", "BOAT"), "C": ("~", "CANOE"),
    "[": ("•", "PERSON"), "_": ("+", "WX"), "W": ("+", "NWS"),
    "#": ("◆", "DIGI"), "&": ("◇", "IGATE"), "r": ("Ψ", "RPTR"), "m": ("Ψ", "RPTR"),
    "-": ("▪", "HOME"), "y": ("▪", "HOME"), "`": ("▪", "HOME"),
    "o": ("▫", "EOC"), "h": ("▫", "HOSP"), "+": ("▫", "AID"), "c": ("▫", "ICP"), "d": ("▫", "FIRE"),
    "!": ("▫", "POLICE"), "@": ("@", "STORM"), "\\": ("▵", "DF"),
}
_KIND_ORDER = {"WX": 0, "NWS": 0, "MOBILE": 1, "AIR": 1, "HELO": 1, "BOAT": 1, "PERSON": 1, "BALLOON": 1}


def kind(s: dict) -> str:
    k = SYMBOLS.get((s.get("sym") or "//")[1:], (None, ""))[1]
    if s.get("wx") and k in ("", "HOME"):  # a weather report from a house (or unknown) symbol: a weather station
        return "WX"
    return k or ("OBJ" if s.get("obj") else "STN")


def glyph(s: dict) -> str:
    k = kind(s)
    if k == "MOBILE":  # an arrow along its course while moving, a ring when parked
        crs, spd = s.get("course"), s.get("speed_mph") or 0
        return ARROWS[round(crs % 360 / 45) % 8] if crs and spd > 2 else "○"
    if k == "WX":
        return "+"
    return SYMBOLS.get((s.get("sym") or "//")[1:], (None,))[0] or ("◦" if s.get("obj") else "•")


def temp_label(s: dict) -> str:
    v = (s.get("wx") or {}).get("temp_f")
    return f"{v:.0f}°" if v is not None else ""


def miles(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(min(1.0, math.sqrt(h)))


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def age(s: dict, now: float) -> str:
    a = now - (s.get("t") or now)
    return f"{a:.0f}S" if a < 90 else f"{a // 60:.0f}M" if a < 5400 else f"{a // 3600:.0f}H"


def stations(app) -> list[dict]:
    return ((getattr(app, "aprs", None) or {}).get("stations")) or []


def enabled(app) -> bool:
    """APRS is on for this location (the collector runs the "aprs" job)."""
    return "aprs" in (((getattr(app, "data", None) or {}).get("sources")) or {})


def stamp(app) -> tuple:
    """Changes whenever the APRS picture on the radar map would; part of RadarPanel's frame cache key."""
    view = getattr(app, "aprs_view", "OFF")
    return (view, (getattr(app, "aprs", None) or {}).get("now")) if view in ("WX", "ALL") else (view,)


def nearest(app, now: float) -> list[tuple[float, float, dict]]:
    """(miles, bearing, station) for every station, nearest first."""
    home = (getattr(app, "aprs", None) or {}).get("home")
    if not home:
        return []
    out = [(miles(home[0], home[1], s["lat"], s["lon"]), bearing(home[0], home[1], s["lat"], s["lon"]), s)
           for s in stations(app)]
    return sorted(out, key=lambda r: r[0])


# --- drawing stations onto a map canvas ---------------------------------------------------------------------------

def draw_stations(c: Canvas, t, rows: list[dict], proj, mode: str, now: float, trail_min: float = TRAIL_MIN):
    """Stations onto canvas c. proj(lon, lat) -> dot (x, y) on c. mode WX: weather stations only, as temperatures;
    ALL or MAP: every station with its glyph, and a callsign (or temperature) where it fits without covering text."""
    pick = []
    for s in rows:
        k = kind(s)
        if mode == "WX" and not (k in ("WX", "NWS") and temp_label(s)):
            continue
        x, y = proj(s["lon"], s["lat"])
        cx, cy = int(x) // 2, int(y) // 4
        if 0 <= cx < c.w and 0 <= cy < c.h:
            pick.append((_KIND_ORDER.get(k, 2), -(s.get("t") or 0), cx, cy, k, s))
    pick.sort(key=lambda r: r[:2])
    if mode != "WX":  # tracks first, underneath everything
        for *_, k, s in pick:
            pts = [p for p in s.get("track") or [] if now - p[2] < trail_min * 60]
            if len(pts) > 1:
                c.polyline([proj(p[1], p[0]) for p in pts], t.dim, 4)
    placed = []
    for _, _, cx, cy, k, s in pick:  # glyphs, never over existing text (town names, the home mark)
        if c.text[cy][cx]:
            continue
        old = now - (s.get("t") or now) > STALE_MIN * 60
        if mode == "WX":
            lab = temp_label(s)
            x0 = max(0, min(cx - len(lab) // 2, c.w - len(lab)))
            if _free(c, x0, x0 + len(lab), cy, -1):
                c.put(x0, cy, lab, t.dim if old else t.bright)
            continue
        c.put(cx, cy, glyph(s), t.dim if old else (t.bright if k in ("WX", "NWS") else t.accent))
        placed.append((cx, cy, k, s, old))
    for cx, cy, k, s, old in placed:  # then labels, best first, wherever they fit
        lab = temp_label(s) if k in ("WX", "NWS") and mode != "MAP" else s["call"]
        if k in ("WX", "NWS") and mode == "MAP" and temp_label(s):
            lab = f"{s['call']} {temp_label(s)}"
        for x0 in (cx + 1, cx - len(lab) - 1):  # right of the glyph, else left, with a blank cell around it
            if x0 >= 0 and x0 + len(lab) <= c.w and _free(c, x0 - 1, x0 + len(lab) + 1, cy, cx):
                c.put(x0, cy, lab, t.dim if old else (t.bright if k in ("WX", "NWS") else t.fg))
                break


def _free(c: Canvas, x0: int, x1: int, y: int, skip: int) -> bool:
    return all(x == skip or not c.text[y][x] for x in range(max(0, x0), min(c.w, x1)))


# --- the APRS map --------------------------------------------------------------------------------------------------

class AprsPanel(Panel):
    """The swap-in APRS map: home in the middle, range rings, map lines, stations, and the heard list."""

    SIDE = 44   # heard list width, shown when the panel is at least 120 columns

    def view(self) -> tuple[int, int, int]:
        """Map size in dots (w, h) and the heard-list width in cells."""
        side = self.SIDE if self.size.width >= 120 else 0
        return max(2, (self.size.width - 2 - side) * 2), max(2, (self.size.height - 2) * 4), side

    def bbox(self, home) -> tuple[float, float, float, float]:
        """Map box: home in the middle, the nearest edge at the chosen range."""
        W, H, _ = self.view()
        rng = getattr(self.app, "aprs_range", 25)
        lat, lon = home
        k = math.cos(math.radians(lat))
        if W >= H:
            dlat = rng / 69.0
            dlon = dlat / k * W / H
        else:
            dlon = rng / 69.0 / k
            dlat = dlon * k * H / W
        return lon - dlon, lon + dlon, lat - dlat, lat + dlat

    def draw(self, c, t, w, h):
        app = self.app
        now = time.time()
        data = getattr(app, "aprs", None) or {}
        geo = getattr(app, "geo", None)
        st = data.get("status") or {}
        rng = getattr(app, "aprs_range", 25)
        rows = stations(app)
        server = st.get("server") or "APRS-IS"
        c.box(0, 0, w, h, t.dim, f"APRS · {server} · {len(rows)} STATIONS", t.bright,
              right=f"RANGE {rng} MI [ ]" if rows else "")
        if not getattr(app, "data", None):
            c.put(3, 2, "WAITING FOR THE COLLECTOR…", t.dim)
            return
        if not enabled(app):
            c.put(3, 2, "THIS LOCATION'S COLLECTOR ISN'T RUNNING APRS", t.dim)
            for i, line in enumerate(("SET APRS_CALL (AND APRS_LOCATIONS) IN KIOSK.CONF, THEN SUDO BASH KIOSK/SETUP.SH",
                                      "OR IN .ENV, THEN SCRIPTS/GEN-COMPOSE.PY AND DOCKER COMPOSE UP -D --BUILD")):
                c.put(3, 4 + i, line[: w - 5], t.dim)
            return
        if not geo or not data.get("home"):
            c.put(3, 2, "WAITING FOR APRS…", t.dim)
            return
        W, H, side = self.view()
        mw = w - side
        lon0, lon1, lat0, lat1 = self.bbox(data["home"])

        def P(lon, lat):
            return (lon - lon0) / (lon1 - lon0) * (W - 1), (lat1 - lat) / (lat1 - lat0) * (H - 1)

        sub = Canvas(mw - 2, h - 2)
        grid = getattr(app, "aprs_wxgrid", None)
        fr = ((grid or {}).get("frames") or [None])[-1]
        if fr and (grid.get("w"), grid.get("h")) == (W, H):  # latest radar frame, sparse, underneath
            for y, row in enumerate(fr["rows"]):
                if row.count("0") == len(row):
                    continue
                for x, ch in enumerate(row):
                    if ch != "0":
                        lv = int(ch) if ch.isdigit() else "abcdef".index(ch) + 1
                        if BAYER[y & 3][x & 1] < min(lv, 5):
                            sub.dot(x, y, t.snow if ch.isalpha() else t.levels[min(lv, 5) - 1], 0)
        for ln in geo.get("roads", []):
            sub.polyline([P(*p) for p in ln], t.c("faint"), 1)
        for key in ("border", "state"):
            for ln in geo.get(key, []):
                sub.polyline([P(*p) for p in ln], t.dim, 2, dash=2)
        for key in ("lakes", "coast"):
            for ln in geo.get(key, []):
                sub.polyline([P(*p) for p in ln], t.c("land"), 3)
        hlat, hlon = data["home"]
        hx, hy = P(hlon, hlat)
        step = next((s for s in (1, 2, 5, 10, 25, 50) if rng / s <= 3), 50)
        for k in range(1, int(rng * 1.5 / step) + 1):
            ry = k * step / 69.0 / (lat1 - lat0) * (H - 1)
            rx = k * step / 69.0 / math.cos(math.radians(hlat)) / (lon1 - lon0) * (W - 1)
            n = int(2 * math.pi * max(rx, ry) / 3)
            for i in range(0, n, 2):
                a = 2 * math.pi * i / n
                sub.dot(int(hx + rx * math.cos(a)), int(hy + ry * math.sin(a)), t.dim, 1)
            lx, ly = int(hx + rx * 0.71) // 2 + 1, int(hy - ry * 0.71) // 4
            if 0 <= ly < sub.h - 1:
                sub.put(lx, ly, f"{k * step:g} MI", t.dim)
        for name, lat, lon in geo.get("towns", []):
            x, y = P(lon, lat)
            cx, cy = int(x) // 2, int(y) // 4
            if 0 <= cx < sub.w - 2 and 0 <= cy < sub.h:
                sub.put(cx, cy, "·" + name if cx < sub.w - len(name) - 2 else name + "·", t.c("faint"))
        mark = "HOME" if getattr(app, "loc", "") == "home" else self.label("name", "HERE")
        sub.put(int(hx) // 2, int(hy) // 4, "⌂ " + mark, t.bright)
        draw_stations(sub, t, rows, P, "MAP", now, trail_min=60)
        for y in range(sub.h):  # paste the map into the box
            for x in range(sub.w):
                if sub.text[y][x]:
                    c.put(1 + x, 1 + y, sub.chars[y][x], sub.cstyle[y][x])
                elif sub.dots[y][x]:
                    c.dots[1 + y][1 + x], c.dstyle[1 + y][1 + x] = sub.dots[y][x], sub.dstyle[y][x]
        if side:
            self._side(c, t, mw, w, h, now)
        last = max(rows, key=lambda s: s.get("t") or 0, default=None)
        if last:  # the newest packet, in the bottom border
            m = miles(hlat, hlon, last["lat"], last["lon"])
            note = (last.get("comment") or last.get("status") or "").upper()
            tick = (f" LAST {hm(datetime.fromtimestamp(last['t'], panels.TZ))} {last['call']} · {m:.0f} MI "
                    f"{compass_name(bearing(hlat, hlon, last['lat'], last['lon']))}" + (f" · {note}" if note else ""))
            c.put(2, h - 1, tick[: mw - 4].rstrip() + " ", t.fg)

    def _side(self, c, t, x0, w, h, now):
        for yy in range(1, h - 1):
            c.put(x0, yy, "│", t.c("faint"))
        x = x0 + 2
        c.put(x + 2, 1, f"{'HEARD':<10}{'KIND':<7}{'MI':>4} {'DIR':<4}{'AGE':>4}", t.dim)
        rows = sorted(stations(self.app), key=lambda s: -(s.get("t") or 0))
        home = (getattr(self.app, "aprs", None) or {}).get("home")
        n = h - 6
        for i, s in enumerate(rows[: max(0, n)]):
            m = miles(home[0], home[1], s["lat"], s["lon"])
            b = compass_name(bearing(home[0], home[1], s["lat"], s["lon"]))
            old = now - (s.get("t") or now) > STALE_MIN * 60
            line = f"{s['call'][:9]:<10}{kind(s)[:6]:<7}{m:>4.0f} {b:<4}{age(s, now):>4}"
            c.put(x, 2 + i, glyph(s), t.dim if old else t.accent)
            c.put(x + 2, 2 + i, line[: w - x - 4], t.dim if old else t.fg)
        st = (getattr(self.app, "aprs", None) or {}).get("status") or {}
        rf = sum(1 for s in rows if s.get("rf"))
        c.put(x, h - 3, f"PACKETS {st.get('packets', 0):,} · VIA RF {rf * 100 // max(1, len(rows))}%"[: w - x - 2],
              t.dim)
        c.put(x, h - 2, f"FILTER {(getattr(self.app, 'aprs', None) or {}).get('range_km', 0):.0f} KM"
                        f" · LOGIN {(getattr(self.app, 'aprs', None) or {}).get('call') or '--'} RX ONLY"[: w - x - 2],
              t.dim)


# --- rotating extras slots (ExtrasPanel) --------------------------------------------------------------------------

def draw_heard(panel, c, t, w, h, el, dots):
    """APRS HEARD: the stations heard most recently, with distance, direction and their comment."""
    app = panel.app
    now = time.time()
    st = (getattr(app, "aprs", None) or {}).get("status") or {}
    rows = sorted(stations(app), key=lambda s: -(s.get("t") or 0))
    c.box(0, 0, w, h, t.dim, f"APRS HEARD · {st.get('server') or 'APRS-IS'}", t.bright,
          right=f"{len(rows)} STATIONS  {dots}")
    if not rows:
        c.put(3, 2, "WAITING FOR APRS…", t.dim)
        return
    home = app.aprs["home"]
    c.put(2, 1, f"  {'CALL':<10}{'KIND':<7}{'MI':>4} {'DIR':<4}{'AGE':>4}  COMMENT", t.dim)
    for i, s in enumerate(rows[: h - 3]):
        m = miles(home[0], home[1], s["lat"], s["lon"])
        b = compass_name(bearing(home[0], home[1], s["lat"], s["lon"]))
        note = (temp_label(s) + " " if temp_label(s) else "") + (s.get("comment") or s.get("status") or "")
        old = now - (s.get("t") or now) > STALE_MIN * 60
        c.put(2, 2 + i, glyph(s), t.dim if old else t.accent)
        c.put(4, 2 + i, f"{s['call'][:9]:<10}{kind(s)[:6]:<7}{m:>4.0f} {b:<4}{age(s, now):>4}  ", t.dim if old else t.fg)
        c.put(36, 2 + i, note.upper()[: w - 38], t.dim)


def draw_wx(panel, c, t, w, h, el, dots):
    """APRS WEATHER: nearby APRS/CWOP weather stations, nearest first, under the official observation."""
    app = panel.app
    now = time.time()
    rows = [r for r in nearest(app, now) if r[2].get("wx")]
    c.box(0, 0, w, h, t.dim, "APRS WEATHER STATIONS · NEAREST FIRST", t.bright, right=f"{len(rows)} REPORTING  {dots}")
    if not rows:
        c.put(3, 2, "NO APRS WEATHER STATIONS HEARD YET", t.dim)
        return
    hdr = f"{'STATION':<10}{'MI':>4} {'DIR':<4}{'TEMP':>5}{'HUM':>5}  {'WIND':<10}{'PRESS':>6}{'RAIN':>6}{'AGE':>5}"
    c.put(3, 1, hdr, t.dim)
    obs = panel.src("obs") or {}
    y = 2
    if obs.get("temp_f") is not None:
        wind = f"{_dir(obs.get('wind_dir'))} {fmt(obs.get('wind_mph'), 0)}"
        c.put(3, y, f"{(obs.get('station') or 'OFFICIAL')[:9]:<10}{'':>4} {'':<4}{fmt(obs.get('temp_f'), 0, '°'):>5}"
                    f"{fmt(obs.get('humidity'), 0, '%'):>5}  {wind:<10}{fmt(obs.get('pressure_inhg'), 2):>6}", t.bright)
        y += 1
    for m, b, s in rows[: h - 1 - y]:
        wx = s["wx"]
        p = wx.get("pressure_mb")
        wind = f"{_dir(wx.get('wind_dir'))} {fmt(wx.get('wind_mph'), 0)}" + \
            (f"G{wx['gust_mph']:.0f}" if (wx.get("gust_mph") or 0) > (wx.get("wind_mph") or 0) + 3 else "")
        old = now - (s.get("wx_t") or s.get("t") or now) > STALE_MIN * 60
        c.put(3, y, f"{s['call'][:9]:<10}{m:>4.0f} {compass_name(b):<4}{fmt(wx.get('temp_f'), 0, '°'):>5}"
                    f"{fmt(wx.get('humidity'), 0, '%'):>5}  {wind:<10}{fmt(p * 0.02953 if p else None, 2):>6}"
                    f"{fmt(wx.get('rain_today_in'), 2):>6}{age(s, now):>5}", t.dim if old else t.fg)
        y += 1


def _dir(deg) -> str:
    return compass_name(deg) or "--"
