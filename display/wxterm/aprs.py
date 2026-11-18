"""APRS: stations heard on APRS-IS around home, drawn on the weather map.

By Steve Miller (KC1AWV). A layer on the big map (MapPanel in flights.py): `x` cycles OFF, WX (APRS weather
stations' temperatures, like a surface map) and ALL (every station as a symbol with its callsign where it fits, a
30-minute track behind moving stations, stations not heard for 30 minutes dimmed).

Data comes from the collector's /api/aprs (collector/wxcollect/aprs.py). Two rotating extras slots, APRS HEARD and
APRS WEATHER, read the same data.
"""
from __future__ import annotations

import math
import time

from .canvas import Canvas
from .glyphs import compass_name
from .panels import fmt

VIEWS = ("OFF", "WX", "ALL")
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
    ALL: every station with its glyph, and a callsign (or temperature) where it fits without covering text."""
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
        for *_, s in pick:
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
        c.put(4, 2 + i, f"{s['call'][:9]:<10}{kind(s)[:6]:<7}{m:>4.0f} {b:<4}{age(s, now):>4}  ",
              t.dim if old else t.fg)
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
