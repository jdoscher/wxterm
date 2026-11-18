"""Earthquakes (`e`) and wildfires (`i`) on the map, from free sources with no key.

The collector hands over worldwide lists (/api/quakes: USGS + EMSC, /api/fires: NIFC + GDACS; see
collector/wxcollect/hazards.py) and the display draws what falls on its own map:

- Earthquakes: a braille ring whose size grows with magnitude, brightest when new (warn colour in the first hour,
  accent for the day, dim for the rest of the week), with "M4.2" beside the bigger ones. Everything from the past
  day at QUAKE_MIN_MAG and up, and M2.5+ for the past week.
- Wildfires: US perimeters as outlines with a diagonal hatch, and a ▲ at each incident with its name, size and
  containment. Large fires elsewhere come from GDACS as a ▲ and their burnt area.

Two rotating panel pages list the quakes and fires near the location, and the map's top border notes the newest quake
and the number of fires on the map.
"""
from __future__ import annotations

import math
import os
import time

from .canvas import Canvas
from .glyphs import compass_name

LIST_MI = 300                    # the panel pages list what's within this many miles of the location
NEW_S = 3600                     # a quake this young is drawn in the warn colour and named in the border
DAY_S = 86400
WEEK_MIN_MAG = 2.5               # quakes older than a day are drawn only from this magnitude


def min_mag() -> float:
    try:
        return float(os.environ.get("WXTERM_QUAKE_MIN_MAG", "1.5"))
    except ValueError:
        return 1.5


def miles(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(min(1.0, math.sqrt(h)))


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def ago(t: float | None, now: float) -> str:
    if not t:
        return "--"
    a = max(0, now - t)
    return f"{a // 60:.0f}M" if a < 3600 else f"{a // 3600:.0f}H" if a < 2 * DAY_S else f"{a // DAY_S:.0f}D"


def shown_quakes(quakes: list[dict], now: float, mmin: float | None = None) -> list[dict]:
    """The quakes the map draws: the past day from mmin up, the rest of the week from M2.5."""
    mmin = min_mag() if mmin is None else mmin
    out = []
    for q in quakes or []:
        age = now - q["t"]
        if age < 0 or age > 7 * DAY_S:
            continue
        if q["mag"] >= (mmin if age < DAY_S else max(WEEK_MIN_MAG, mmin)):
            out.append(q)
    return out


def ring_dots(mag: float) -> float:
    """Ring radius in braille dots: about 3 for M1, 6 for M3, 9 for M5, 11 for M7."""
    return 1.5 + 1.4 * max(0.0, mag)


def in_box(lat, lon, bbox) -> bool:
    lon0, lon1, lat0, lat1 = bbox
    return lat0 <= lat <= lat1 and lon0 <= lon <= lon1


# --- drawing onto the map canvas ----------------------------------------------------------------------------------

def _free(c: Canvas, x0: int, x1: int, y: int) -> bool:
    return 0 <= y < c.h and all(not c.text[y][x] for x in range(max(0, x0), min(c.w, x1)))


def _label(c: Canvas, cx: int, cy: int, lab: str, style: str, gap: int = 1) -> bool:
    """Put lab right of (cx, cy), else left, where it covers no other text (a blank cell either side)."""
    for x0 in (cx + gap + 1, cx - gap - len(lab)):
        if x0 >= 0 and x0 + len(lab) <= c.w and _free(c, x0 - 1, x0 + len(lab) + 1, cy):
            c.put(x0, cy, lab, style)
            return True
    return False


def draw_quakes(c: Canvas, t, quakes: list[dict], proj, now: float, mmin: float | None = None) -> int:
    """Rings for the quakes on canvas c (proj(lon, lat) -> dot x, y). Oldest and smallest first, so the newest and
    biggest end up on top. Returns how many were drawn."""
    W, H = c.w * 2, c.h * 4
    todo = []
    for q in shown_quakes(quakes, now, mmin):
        x, y = proj(q["lon"], q["lat"])
        r = ring_dots(q["mag"])
        if -r <= x < W + r and -r <= y < H + r:
            todo.append((x, y, r, q))
    todo.sort(key=lambda v: (v[3]["t"], v[3]["mag"]))
    for x, y, r, q in todo:
        age = now - q["t"]
        st = t.warn if age < NEW_S else t.accent if age < DAY_S else t.dim
        prio = 14 if age < NEW_S else 13 if age < DAY_S else 12   # over the radar (6-10) and roads
        n = max(10, int(2 * math.pi * r))
        for k in range(n):
            a = 2 * math.pi * k / n
            dx, dy = x + r * math.cos(a), y + r * math.sin(a) * 0.9   # braille dots are a little taller than wide
            if 0 <= dx < W and 0 <= dy < H:
                c.dot(dx, dy, st, prio)
        if 0 <= x < W and 0 <= y < H:
            c.dot(x, y, st, prio)
    # labels: biggest and newest first, where they fit
    for x, y, r, q in sorted(todo, key=lambda v: (-(now - v[3]["t"] < NEW_S), -v[3]["mag"])):
        age = now - q["t"]
        if q["mag"] < WEEK_MIN_MAG and age >= NEW_S:
            continue
        cx, cy = int(x) // 2, int(y) // 4
        if not (0 <= cx < c.w and 0 <= cy < c.h):
            continue
        st = t.warn if age < NEW_S else t.bright if age < DAY_S else t.dim
        lab = f"M{q['mag']:.1f}" + (f" {ago(q['t'], now)} AGO" if age < NEW_S else "")
        _label(c, cx, cy, lab, st, gap=int(r // 2) + 1)
    return len(todo)


def _fill_rings(rings_px: list[list[tuple[float, float]]], W: int, H: int):
    """Dot (x, y) cells inside the polygon (even-odd over all rings), scanned row by row."""
    ys = [p[1] for r in rings_px for p in r]
    if not ys:
        return
    for y in range(max(0, int(min(ys))), min(H, int(max(ys)) + 1)):
        yc = y + 0.5
        xs = []
        for r in rings_px:
            for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1], strict=False):
                if (y0 <= yc < y1) or (y1 <= yc < y0):
                    xs.append(x0 + (yc - y0) * (x1 - x0) / (y1 - y0))
        xs.sort()
        for a, b in zip(xs[::2], xs[1::2], strict=False):
            for x in range(max(0, int(math.ceil(a - 0.5))), min(W, int(b + 0.5))):
                yield x, y


def draw_fires(c: Canvas, t, fires: list[dict], perims: list[dict], proj) -> int:
    """Perimeters (outline plus a diagonal hatch) and a ▲ with a label for each fire on canvas c. Returns how many
    fires are on the map."""
    W, H = c.w * 2, c.h * 4
    for p in perims or []:
        rings = [[proj(x, y) for x, y in ring] for ring in p.get("rings") or []]
        xs = [pt[0] for r in rings for pt in r]
        ys = [pt[1] for r in rings for pt in r]
        if not xs or max(xs) < 0 or min(xs) >= W or max(ys) < 0 or min(ys) >= H:
            continue
        out = (p.get("contained") or 0) >= 100
        st = t.dim if out else t.warn
        for x, y in _fill_rings(rings, W, H):
            if (x + y) % 4 == 0:
                c.dot(x, y, st, 2)
        for r in rings:
            pts = r + r[:1]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
                if max(x0, x1) >= 0 and min(x0, x1) < W and max(y0, y1) >= 0 and min(y0, y1) < H:
                    c.line(x0, y0, x1, y1, st, 11)   # outline over the rain; the hatch stays under it
    on = []
    for f in fires or []:
        x, y = proj(f["lon"], f["lat"])
        cx, cy = int(x) // 2, int(y) // 4
        if 0 <= cx < c.w and 0 <= cy < c.h:
            on.append((-(f.get("acres") or 0), cx, cy, f))
    on.sort(key=lambda v: v[0])   # biggest first: they get the marker and the label when two share a spot
    for _, cx, cy, f in on:
        if c.text[cy][cx]:
            continue
        out = (f.get("contained") or 0) >= 100
        c.put(cx, cy, "▲", t.dim if out else t.warn)
        _label(c, cx, cy, fire_label(f), t.dim if out else t.bright)
    return len(on)


def fire_label(f: dict) -> str:
    if f.get("src") == "GDACS":
        return f"WILDFIRE {f['ha']:,.0f} HA" if f.get("ha") else "WILDFIRE"
    lab = f["name"][:22]
    if (f.get("acres") or 0) >= 1:   # brand-new incidents report 0 or a fraction of an acre: leave it off
        lab += f" {f['acres']:,.0f} AC"
    if f.get("contained") is not None:
        lab += f" {f['contained']:.0f}%"
    return lab


# --- app-side helpers ---------------------------------------------------------------------------------------------

def quake_list(app) -> list[dict]:
    return ((getattr(app, "quakes", None) or {}).get("quakes")) or []


def fire_list(app) -> list[dict]:
    return ((getattr(app, "fires", None) or {}).get("fires")) or []


def perim_list(app) -> list[dict]:
    return ((getattr(app, "fires", None) or {}).get("perimeters")) or []


def border_notes(app, bbox, now: float) -> list[str]:
    """Top-border notes: the newest quake on the map if it's under an hour old (else the day's count), and the
    number of fires on the map."""
    out = []
    if getattr(app, "quake_on", False) and bbox:
        on = [q for q in shown_quakes(quake_list(app), now) if in_box(q["lat"], q["lon"], bbox)]
        new = [q for q in on if now - q["t"] < NEW_S]
        if new:
            q = max(new, key=lambda q: q["mag"])
            out.append(f"M{q['mag']:.1f} QUAKE {ago(q['t'], now)} AGO")
        elif on:
            day = sum(1 for q in on if now - q["t"] < DAY_S)
            n = day or len(on)
            out.append(f"{n} QUAKE{'S' if n != 1 else ''} {'TODAY' if day else 'THIS WEEK'}")
    if getattr(app, "fire_on", False) and bbox:
        n = sum(1 for f in fire_list(app) if in_box(f["lat"], f["lon"], bbox))
        if n:
            out.append(f"{n} FIRE{'S' if n != 1 else ''}")
    return out


def near(rows: list[dict], home, radius_mi: float = LIST_MI) -> list[tuple[float, float, dict]]:
    """(miles, bearing, row) within radius of home, nearest first."""
    out = []
    for r in rows:
        m = miles(home[0], home[1], r["lat"], r["lon"])
        if m <= radius_mi:
            out.append((m, bearing(home[0], home[1], r["lat"], r["lon"]), r))
    return sorted(out, key=lambda v: v[0])


# --- rotating extras pages ----------------------------------------------------------------------------------------

def _home(panel):
    geo = getattr(panel.app, "geo", None) or {}
    return geo.get("home")


def draw_quake_page(panel, c, t, w, h, el, dots):
    """EARTHQUAKES: the past week's quakes within LIST_MI of the location, newest first, plus the day's largest
    anywhere in the world."""
    app = panel.app
    now = time.time()
    data = getattr(app, "quakes", None) or {}
    src = " + ".join(data.get("sources") or []) or "USGS"
    c.box(0, 0, w, h, t.dim, f"EARTHQUAKES · WITHIN {LIST_MI} MI · {src}", t.bright, right=dots)
    home = _home(panel)
    if not data or not home:
        c.put(3, 2, "WAITING FOR EARTHQUAKE DATA…", t.dim)
        return
    allq = quake_list(app)
    rows = [v for v in near(shown_quakes(allq, now), home)]
    rows.sort(key=lambda v: -v[2]["t"])
    world = [q for q in allq if now - q["t"] < DAY_S]
    y = 1
    if world:
        big = max(world, key=lambda q: q["mag"])
        c.put(3, y, f"LARGEST TODAY: M{big['mag']:.1f} {big['place'].upper()} · {ago(big['t'], now)} AGO"[: w - 6],
              t.bright)
        y += 1
    if not rows:
        c.put(3, y + 1, f"NO EARTHQUAKES WITHIN {LIST_MI} MI THIS WEEK", t.dim)
        return
    c.put(3, y, f"{'AGE':>4} {'MAG':>4} {'DEPTH':>6} {'MI':>4} {'DIR':<4} PLACE", t.dim)
    y += 1
    for m, b, q in rows[: max(0, h - 1 - y)]:
        age = now - q["t"]
        st = t.warn if age < NEW_S else t.fg if age < DAY_S else t.dim
        depth = f"{q['depth_km']:.0f} KM" if q.get("depth_km") is not None else "--"
        c.put(3, y, f"{ago(q['t'], now):>4} {q['mag']:>4.1f} {depth:>6} {m:>4.0f} {compass_name(b):<4} "
                    f"{q['place'].upper()}"[: w - 5], st)
        y += 1


def draw_fire_page(panel, c, t, w, h, el, dots):
    """WILDFIRES: fires within LIST_MI of the location, nearest first."""
    app = panel.app
    now = time.time()
    data = getattr(app, "fires", None) or {}
    src = " + ".join(data.get("sources") or []) or "NIFC"
    c.box(0, 0, w, h, t.dim, f"WILDFIRES · WITHIN {LIST_MI} MI · {src}", t.bright, right=dots)
    home = _home(panel)
    rows = near(fire_list(app), home) if home else []
    if not rows:
        c.put(3, 2, f"NO ACTIVE WILDFIRES WITHIN {LIST_MI} MI", t.dim)
        return
    c.put(3, 1, f"{'NAME':<22} {'ACRES':>9} {'CONT':>5} {'MI':>4} {'DIR':<4} {'FOUND':>5}", t.dim)
    for i, (m, b, f) in enumerate(rows[: h - 3]):
        out = (f.get("contained") or 0) >= 100
        acres = f"{f['acres']:,.0f}" if (f.get("acres") or 0) >= 1 else "<1" if f.get("acres") is not None else "--"
        cont = f"{f['contained']:.0f}%" if f.get("contained") is not None else "--"
        name = f["name"] if f.get("src") != "GDACS" else f"WILDFIRE ({f.get('country', '')})".upper()
        c.put(3, 2 + i, f"{name[:22]:<22} {acres:>9} {cont:>5} {m:>4.0f} {compass_name(b):<4} "
                        f"{ago(f.get('discovered'), now):>5}"[: w - 5], t.dim if out else t.fg)
