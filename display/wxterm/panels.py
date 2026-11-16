"""The screen's panels. Each one draws itself onto a Canvas sized to the widget, so layout follows the terminal."""
from __future__ import annotations

import math
import textwrap
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from textual.widget import Widget

from . import glyphs
from .canvas import BAYER, Canvas
from .themes import Theme

TZ = ZoneInfo("America/New_York")  # replaced by the collector's home tz (set_tz) once data arrives


def set_tz(name: str | None):
    global TZ
    if name:
        TZ = ZoneInfo(name)


# --- helpers -----------------------------------------------------------------

def ptime(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)


def hm(d: datetime | None) -> str:
    if d is None:
        return "--"
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d}{'A' if d.hour < 12 else 'P'}"


def hour_label(d: datetime) -> str:
    h = d.hour % 12 or 12
    return f"{h}{'A' if d.hour < 12 else 'P'}"


def ago(iso: str | None) -> str:
    d = ptime(iso)
    if d is None:
        return "--"
    s = (datetime.now(TZ) - d).total_seconds()
    return f"{int(s // 60)}M" if s < 5400 else f"{int(s // 3600)}H"


def fmt(v, nd=0, unit=""):
    return "--" if v is None else f"{v:.{nd}f}{unit}"


class Panel(Widget):
    """Base: subclasses implement draw(canvas, theme, w, h)."""

    @property
    def pal(self) -> Theme:
        return self.app.pal  # type: ignore[attr-defined]

    def src(self, name: str):
        d = getattr(self.app, "data", None) or {}
        ent = (d.get("sources") or {}).get(name) or {}
        return ent.get("data")

    def label(self, key: str, default: str = "") -> str:
        d = getattr(self.app, "data", None) or {}
        return (d.get("labels") or {}).get(key) or default

    def stale(self, name: str) -> bool:
        d = getattr(self.app, "data", None) or {}
        ent = (d.get("sources") or {}).get(name)
        if not ent or not ent.get("ok_at"):
            return True
        age = (datetime.now(TZ) - ptime(ent["ok_at"])).total_seconds()
        return age > 3 * (ent.get("interval") or 600) + 120

    def render(self):
        w, h = self.size.width, self.size.height
        c = Canvas(w, h)
        try:
            self.draw(c, self.pal, w, h)
        except Exception as e:  # noqa: BLE001 - a broken panel shouldn't take down the screen
            c.box(0, 0, w, h, self.pal.dim, "ERROR", self.pal.warn)
            for i, line in enumerate(textwrap.wrap(f"{type(e).__name__}: {e}", max(10, w - 4))[: h - 2]):
                c.put(2, 1 + i, line, self.pal.warn)
        return c.render()

    def draw(self, c: Canvas, t: Theme, w: int, h: int):  # pragma: no cover - abstract
        raise NotImplementedError


# --- header: clock, current conditions, wind, alerts ---------------------------

class NowPanel(Panel):
    def draw(self, c, t, w, h):
        now = datetime.now(TZ)
        obs = self.src("obs") or {}
        om = self.src("openmeteo") or {}
        cur = om.get("current") or {}
        daily = om.get("daily") or {}
        c.box(0, 0, w, h, t.dim, self.label("name", "WXTERM"), t.bright,
              right=f"{obs.get('station', '')} OBS {hm(ptime(obs.get('time')))}")
        iw = w - 4

        # date and time, normal size
        c.put(2, 1, now.strftime("%A %B %-d %Y").upper(), t.fg)
        clock = f"{now.hour % 12 or 12}:{now.minute:02d}:{now.second:02d} {'AM' if now.hour < 12 else 'PM'}"
        c.put(w - 2 - len(clock), 1, clock, t.bright)
        a = ((getattr(self.app, "data", None) or {}).get("astro") or {}).get("today") or {}
        c.put(2, 2, f"SUNRISE {hm(ptime(a.get('sunrise')))}   SUNSET {hm(ptime(a.get('sunset')))}"
                    f"   GOLDEN HOUR {hm(ptime(a.get('golden_start')))}", t.dim)
        c.put(1, 3, "─" * (w - 2), t.c("faint"))

        # temperature (big) + conditions
        temp = obs.get("temp_f") if obs.get("temp_f") is not None else cur.get("temperature_2m")
        tstr = f"{temp:.0f}°" if temp is not None else "--°"
        style = getattr(self.app, "bigfont", "segment")
        for r, line in enumerate(glyphs.big(tstr, style)):
            c.put(2, 4 + r, line, t.bright)
        tx = 2 + glyphs.big_width(tstr, style) + 3
        cond = (obs.get("text") or glyphs.WMO.get(cur.get("weather_code"), "")).upper()
        hi = (daily.get("temperature_2m_max") or [None, None])[1]
        lo = (daily.get("temperature_2m_min") or [None, None])[1]
        feels = cur.get("apparent_temperature")
        tw = w - tx - 2
        c.put(tx, 4, (cond or "--")[:tw] + ("   OBSERVATION STALE" if self.stale("obs") else ""), t.accent)
        c.put(tx, 5, f"FEELS {fmt(feels, 0, '°')}   HI {fmt(hi, 0, '°')}   LO {fmt(lo, 0, '°')}"[:tw], t.fg)
        c.put(tx, 6, (f"HUMIDITY {fmt(obs.get('humidity') or cur.get('relative_humidity_2m'), 0, '%')}"
                      f"   DEW POINT {fmt(obs.get('dewpoint_f'), 0, '°')}")[:tw], t.fg)
        c.put(tx, 7, f"PRESSURE {fmt(obs.get('pressure_inhg'), 2, ' IN')} {self._ptrend(om)}"[:tw], t.fg)
        c.put(tx, 8, (f"VISIBILITY {fmt(obs.get('visibility_mi'), 0, ' MI')}   UV {fmt(cur.get('uv_index'), 0)}"
                      f"   CLOUD {fmt(cur.get('cloud_cover'), 0, '%')}")[:tw], t.fg)

        # wind, text only
        wd, ws, wg = obs.get("wind_dir"), obs.get("wind_mph"), obs.get("gust_mph")
        if ws is None:
            wd, ws, wg = cur.get("wind_direction_10m"), cur.get("wind_speed_10m"), cur.get("wind_gusts_10m")
        mx = (daily.get("wind_speed_10m_max") or [None, None])[1]
        wind = "WIND CALM" if not ws else " ".join(x for x in ("WIND", glyphs.compass_name(wd), f"{ws:.0f} MPH") if x)
        wind += (f"   GUSTS {wg:.0f}" if wg else "") + f"   MAX TODAY {fmt(mx, 0, ' MPH')}"
        c.put(2, 9, wind[:iw], t.fg)

        # alerts, one line
        al_src = self.src("alerts") or {}
        alerts = al_src.get("alerts") or []
        if al_src.get("unsupported"):
            c.put(2, 10, "NO ALERT FEED OUTSIDE THE US", t.dim)
        elif not alerts:
            c.put(2, 10, "NO ACTIVE ALERTS", t.dim)
        else:
            blink = int(time.time()) % 2 == 0
            sev = any((al.get("severity") or "").lower() in ("severe", "extreme") for al in alerts)
            parts = []
            for al in alerts[:3]:
                ends = ptime(al.get("ends") or al.get("expires"))
                parts.append((al.get("event") or "").upper() +
                             (f" UNTIL {ends.strftime('%a').upper()} {hm(ends)}" if ends else ""))
            c.put(2, 10, ("▲ " + " · ".join(parts))[:iw], t.warn if sev or blink else t.bright)

    @staticmethod
    def _ptrend(om) -> str:
        hr = om.get("hourly") or {}
        times, p = hr.get("time") or [], hr.get("pressure_msl") or []
        now = datetime.now(TZ).strftime("%Y-%m-%dT%H:00")
        if now not in times:
            return ""
        i = times.index(now)
        if i < 3 or p[i] is None or p[i - 3] is None:
            return ""
        d = p[i] - p[i - 3]
        return "▲ RISING" if d > 0.7 else "▼ FALLING" if d < -0.7 else "► STEADY"


# --- radar --------------------------------------------------------------------

DENS = (0, 2, 3, 5, 7, 8)


class RadarPanel(Panel):
    def dot_size(self) -> tuple[int, int]:
        return max(2, (self.size.width - 2) * 2), max(2, (self.size.height - 2) * 4)

    def on_resize(self, _event):
        self.app.request_radar()  # type: ignore[attr-defined]

    def draw(self, c, t, w, h):
        app = self.app
        radar = getattr(app, "radar", None)
        geo = getattr(app, "geo", None)
        W, H = self.dot_size()
        frames = (radar or {}).get("frames") or []
        idx = min(getattr(app, "radar_idx", 0), len(frames) - 1) if frames else -1
        fr = frames[idx] if idx >= 0 else None
        src = (radar or {}).get("source", "--").upper()
        ftime = datetime.fromtimestamp(fr["t"], TZ) if fr else None
        state = "PAUSED" if getattr(app, "radar_paused", False) else "LOOP"
        c.box(0, 0, w, h, t.dim, f"RADAR · {src} COMPOSITE", t.bright,
              right=f"{hm(ftime)}  {idx + 1}/{len(frames)} {state}" if fr else "WAITING FOR DATA")
        if not radar or radar.get("w") != W or radar.get("h") != H or not geo:
            c.put(3, 2, "LOADING RADAR…", t.dim)
            return
        lon0, lon1, lat0, lat1 = radar["bbox"]

        def P(lon, lat):
            return 2 + (lon - lon0) / (lon1 - lon0) * (W - 1), 4 + (lat1 - lat) / (lat1 - lat0) * (H - 1)

        sub = Canvas(w - 2, h - 2)
        sub_p = lambda lon, lat: (P(lon, lat)[0] - 2, P(lon, lat)[1] - 4)  # noqa: E731
        for ln in geo.get("roads", []):
            sub.polyline([sub_p(*p) for p in ln], t.c("faint"), 1)
        for ln in geo.get("lakes", []):  # big lakes (Lake Michigan) are shoreline, so draw them like the coast
            sub.polyline([sub_p(*p) for p in ln], t.c("land"), 3)
        for key in ("border", "state"):
            for ln in geo.get(key, []):
                sub.polyline([sub_p(*p) for p in ln], t.dim, 2, dash=2)
        for ln in geo.get("coast", []):
            sub.polyline([sub_p(*p) for p in ln], t.c("land"), 3)
        hlat, hlon = geo["home"]
        hx, hy = sub_p(hlon, hlat)
        for miles in (25, 50):
            dlat = miles / 69.0
            ry = dlat / (lat1 - lat0) * (H - 1)
            rx = dlat / math.cos(math.radians(hlat)) / (lon1 - lon0) * (W - 1)
            n = int(2 * math.pi * max(rx, ry) / 3)
            for k in range(n):
                if k % 2:
                    continue
                a = 2 * math.pi * k / n
                sub.dot(int(hx + rx * math.cos(a)), int(hy + ry * math.sin(a)), t.dim, 1)
            sub.put(int(hx + rx * 0.71) // 2 + 1, int(hy - ry * 0.71) // 4, f"{miles} MI", t.dim)
        any_rain = False
        if fr:
            levels, snow = t.levels, t.snow
            for y, row in enumerate(fr["rows"]):
                if row.count("0") == len(row):
                    continue
                for x, ch in enumerate(row):
                    if ch == "0":
                        continue
                    lv = int(ch) if ch.isdigit() else "abcdef".index(ch) + 1
                    if BAYER[y & 3][x & 1] < DENS[min(lv, 5)]:
                        sub.dot(x, y, snow if ch.isalpha() else levels[min(lv, 5) - 1], 5 + lv)
                        any_rain = True
        for name, lat, lon in geo.get("towns", []):
            x, y = sub_p(lon, lat)
            cx, cy = int(x) // 2, int(y) // 4
            if 0 <= cx < sub.w - 2 and 0 <= cy < sub.h:
                sub.put(cx, cy, "·" + name if cx < sub.w - len(name) - 2 else name + "·", t.dim)
        mark = "HOME" if getattr(app, "loc", "") == "home" else self.label("name", "HERE")
        sub.put(int(hx) // 2, int(hy) // 4, "⌂ " + mark, t.bright)
        if not any_rain:
            sub.put(1, sub.h - 1, "NO PRECIPITATION IN RANGE", t.dim)
        # legend
        lx = sub.w - 30
        sub.put(lx - 1, sub.h - 1, " " * 31, t.dim)
        sub.put(lx, sub.h - 1, "DBZ 10", t.dim)
        for i, col in enumerate(t.levels):
            sub.put(lx + 7 + i * 4, sub.h - 1, "██", col)
        sub.put(lx + 27, sub.h - 1, "50", t.dim)
        # paste sub into c
        for y in range(sub.h):
            for x in range(sub.w):
                if sub.text[y][x]:
                    c.put(1 + x, 1 + y, sub.chars[y][x], sub.cstyle[y][x])
                elif sub.dots[y][x]:
                    c.dots[1 + y][1 + x] = sub.dots[y][x]
                    c.dstyle[1 + y][1 + x] = sub.dstyle[y][x]
                    c.dprio[1 + y][1 + x] = sub.dprio[y][x]
        # timeline in the bottom border
        if frames:
            tl = "".join("█" if i == idx else "▪" for i in range(len(frames)))
            first, last = (hm(datetime.fromtimestamp(frames[i]["t"], TZ)) for i in (0, -1))
            c.put(2, h - 1, f" {first} {tl} {last} ", t.fg)


# --- 48 hour graph ---------------------------------------------------------------

BARS = " ▁▂▃▄▅▆▇█"


class GraphPanel(Panel):
    def draw(self, c, t, w, h):
        om = self.src("openmeteo") or {}
        hr = om.get("hourly") or {}
        c.box(0, 0, w, h, t.dim, "48 HOURS · TEMP °F ── DEW POINT ┄┄ RAIN CHANCE ▇", t.bright,
              right="OPEN-METEO" + (" STALE" if self.stale("openmeteo") else ""))
        times = [ptime(x) for x in hr.get("time") or []]
        if not times:
            c.put(3, 2, "WAITING FOR DATA…", t.dim)
            return
        now = datetime.now(TZ)
        start, end = now - timedelta(hours=24), now + timedelta(hours=24)
        idx = [i for i, d in enumerate(times) if start - timedelta(hours=1) <= d <= end + timedelta(hours=1)]
        temp = hr["temperature_2m"]
        dew = hr.get("dew_point_2m") or [None] * len(temp)
        pop = hr.get("precipitation_probability") or [0] * len(temp)
        vals = [temp[i] for i in idx if temp[i] is not None] + [dew[i] for i in idx if dew[i] is not None]
        tmin, tmax = math.floor(min(vals) / 5) * 5, math.ceil(max(vals) / 5) * 5
        if tmax - tmin < 10:
            tmax += 5
        x0, y0 = 6, 1
        pw, ph = w - x0 - 2, h - 2 - 3  # plot cells
        PW, PH = pw * 2, ph * 4

        def X(d):
            return x0 * 2 + (d - start).total_seconds() / (end - start).total_seconds() * (PW - 1)

        def Y(v):
            return y0 * 4 + (tmax - v) / (tmax - tmin) * (PH - 1)

        for k in range(0, 5):  # grid + labels
            v = tmin + (tmax - tmin) * k / 4
            yy = Y(v)
            for xx in range(x0 * 2, x0 * 2 + PW, 4):
                c.dot(xx, int(yy), t.c("faint"), 0)
            c.put(1, int(yy) // 4, f"{v:>3.0f}°", t.dim)
        c.polyline([(X(times[i]), Y(dew[i])) for i in idx if dew[i] is not None], t.dim, 2, dash=2)
        c.polyline([(X(times[i]), Y(temp[i])) for i in idx if temp[i] is not None], t.c("line"), 3)
        # peaks
        fut = [i for i in idx if times[i] >= now and temp[i] is not None]
        for pick in (max(fut, key=lambda i: temp[i]), min(fut, key=lambda i: temp[i])) if fut else ():
            cx, cy = int(X(times[pick])) // 2, int(Y(temp[pick])) // 4
            lab = f"{temp[pick]:.0f}° {hour_label(times[pick])}"
            c.put(min(cx - len(lab) // 2, w - len(lab) - 2), cy - 1 if cy > 2 else cy + 1, lab, t.bright)
        # now line
        nx = int(X(now))
        for yy in range(y0 * 4, y0 * 4 + PH, 2):
            c.dot(nx, yy, t.accent, 4)
        c.put(nx // 2 + 1, y0, "NOW", t.accent)
        # rain bars (2 rows) + labels
        by = y0 + ph
        for cx in range(x0, x0 + pw):
            d = start + (end - start) * ((cx - x0 + 0.5) / pw)
            near = min(idx, key=lambda i: abs((times[i] - d).total_seconds()))
            n = round((pop[near] or 0) / 100 * 16)
            lo_, hi_ = min(n, 8), max(0, n - 8)
            if lo_:
                c.put(cx, by + 1, BARS[lo_], t.c("rain"))
            if hi_:
                c.put(cx, by, BARS[hi_], t.c("rain"))
        c.put(1, by + 1, "RAIN", t.dim)
        hr0 = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        d = hr0
        while d <= end:
            if d.hour % 6 == 0:
                cx = int(X(d)) // 2
                lab = d.strftime("%a").upper() if d.hour == 0 else hour_label(d)
                c.put(cx - len(lab) // 2, h - 2, lab, t.fg if d.hour == 0 else t.dim)
            d += timedelta(hours=1)


# --- tides + moon ----------------------------------------------------------------

class TidePanel(Panel):
    def draw(self, c, t, w, h):
        tides = self.src("tides") or {}
        wt = self.src("water_temp") or {}
        sst = ((self.src("marine") or {}).get("current") or {}).get("sea_surface_temperature")
        water = wt.get("temp_f") if wt.get("temp_f") is not None else tides.get("sst_f")
        right = f"WATER {fmt(water, 1, '°F')}" + (" MODEL" if wt.get("model") else "") if water is not None else ""
        if wt.get("temp_f") and sst and abs(wt["temp_f"] - sst) > 6:
            right += " ?"
        c.box(0, 0, w, h, t.dim, (f"MOON · {self.label('tide_name')}" if tides.get("none") else
               f"MOON · TIDE {self.label('tide_name')} {tides.get('station') or ''} FT {tides.get('datum', 'MLLW')}"),
              t.bright, right=right)

        # moon, text only
        astro = (getattr(self.app, "data", None) or {}).get("astro") or {}
        mn = astro.get("moon") or {}
        mw = 24
        if mn:
            nf, nn = mn.get("next_full"), mn.get("next_new")
            day = lambda iso: datetime.fromisoformat(iso).strftime("%b %-d").upper() if iso else "--"  # noqa: E731
            lines = [("MOON", t.dim),
                     (mn.get("phase", ""), t.bright),
                     (f"{mn.get('illumination', 0) * 100:.0f}% LIT · DAY {mn.get('age', 0):.0f}", t.fg),
                     ("WAXING" if mn.get("waxing") else "WANING", t.fg),
                     (f"RISE {hm(ptime(mn.get('rise')))}", t.dim),
                     (f"SET  {hm(ptime(mn.get('set')))}", t.dim),
                     (f"FULL {day(nf)}", t.dim),
                     (f"NEW  {day(nn)}", t.dim)]
            for i, (s_, st) in enumerate(lines):
                if 1 + i < h - 1:
                    c.put(2, 1 + i, s_[: mw - 1], st)
        for yy in range(1, h - 1):
            c.put(mw + 1, yy, "│", t.c("faint"))

        curve = [(ptime(a), v) for a, v in tides.get("curve") or []]
        if tides.get("none"):
            c.put(mw + 4, 2, "NO TIDES HERE", t.dim)
            a = ((getattr(self.app, "data", None) or {}).get("astro") or {})
            for i, key in enumerate(("today", "tomorrow")):
                s_ = a.get(key) or {}
                c.put(mw + 4, 4 + i, f"{key.upper():<9} SUNRISE {hm(ptime(s_.get('sunrise')))}"
                                     f"  SUNSET {hm(ptime(s_.get('sunset')))}", t.fg)
            return
        if not curve:
            c.put(mw + 4, 2, "WAITING FOR TIDES…", t.dim)
            return
        now = datetime.now(TZ)
        start, end = now - timedelta(hours=6), now + timedelta(hours=30)
        pts = [(d, v) for d, v in curve if start <= d <= end]
        if len(pts) < 2:
            c.put(mw + 4, 2, "TIDE DATA OUT OF RANGE", t.warn)
            return
        vmin, vmax = min(v for _, v in pts), max(v for _, v in pts)
        vmin, vmax = math.floor(vmin) - 0.5, math.ceil(vmax) + 0.5
        x0, y0 = mw + 3, 2
        pw, ph = w - x0 - 2, h - y0 - 3
        PW, PH = pw * 2, ph * 4

        def X(d):
            return x0 * 2 + (d - start).total_seconds() / (end - start).total_seconds() * (PW - 1)

        def Y(v):
            return y0 * 4 + (vmax - v) / (vmax - vmin) * (PH - 1)

        # fill + curve
        bottom = y0 * 4 + PH - 1
        prev = None
        for d, v in pts:
            xx, yy = int(X(d)), int(Y(v))
            if prev is None or xx != prev:
                for fy in range(yy + 2, bottom + 1):
                    if BAYER[fy & 3][xx & 1] < 1:
                        c.dot(xx, fy, t.c("faint"), 1)
            prev = xx
        c.polyline([(X(d), Y(v)) for d, v in pts], t.c("water"), 3)
        for k in range(int(math.ceil(vmin)), int(vmax) + 1, 2):
            c.put(x0 - 3 if x0 - 3 > mw + 1 else x0, int(Y(k)) // 4, f"{k:>2}", t.dim)
        # hi/lo labels
        for a, v, typ in tides.get("hilo") or []:
            d = ptime(a)
            if not d or not (start <= d <= end):
                continue
            cx, cy = int(X(d)) // 2, int(Y(v)) // 4
            lab = f"{typ} {v:.1f}' {hm(d)}"
            lx = max(x0, min(cx - len(lab) // 2, w - len(lab) - 2))
            c.put(lx, cy - 1 if typ == "H" else min(cy + 1, h - 3), lab, t.bright if typ == "H" else t.fg)
        # now
        nowv = min(pts, key=lambda p: abs((p[0] - now).total_seconds()))
        later = min(pts, key=lambda p: abs((p[0] - now - timedelta(minutes=30)).total_seconds()))
        arrow = "▲ RISING" if later[1] > nowv[1] else "▼ FALLING"
        nx = int(X(now))
        for yy in range(y0 * 4, bottom, 2):
            c.dot(nx, yy, t.accent, 4)
        c.put(min(nx // 2 + 1, w - 22), 1, f"NOW {nowv[1]:.1f}' {arrow}", t.accent)
        d = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        while d <= end:
            if d.hour % 6 == 0:
                cx = int(X(d)) // 2
                lab = d.strftime("%a").upper() if d.hour == 0 else hour_label(d)
                c.put(cx - len(lab) // 2, h - 2, lab, t.fg if d.hour == 0 else t.dim)
            d += timedelta(hours=1)


# --- 5 day, text only -------------------------------------------------------------------

class DaysPanel(Panel):
    DAYS = 5

    def draw(self, c, t, w, h):
        fc = self.src("forecast") or {}
        per = fc.get("periods") or []
        c.box(0, 0, w, h, t.dim, f"{self.DAYS}-DAY FORECAST · {self.label('forecast_src', 'NWS')}", t.bright,
              right=f"UPDATED {hm(ptime(fc.get('updated')))}" + (" STALE" if self.stale("forecast") else ""))
        if not per:
            c.put(2, 1, "WAITING FOR FORECAST…", t.dim)
            return
        days: list[dict] = []
        for p in per:
            d = ptime(p["startTime"]).date()
            if not days or days[-1]["date"] != d:
                days.append({"date": d})
            days[-1]["day" if p["isDaytime"] else "night"] = p
        today = datetime.now(TZ).date()
        for i, dd in enumerate(days[: min(self.DAYS, h - 2)]):
            y = 1 + i
            main = dd.get("day") or dd.get("night")
            name = ("TODAY" if dd.get("day") else "TONIGHT") if dd["date"] == today else \
                dd["date"].strftime("%a %-d").upper()
            hi = (dd.get("day") or {}).get("temperature")
            lo = (dd.get("night") or {}).get("temperature")
            pop = max((dd.get(k) or {}).get("pop") or 0 for k in ("day", "night"))
            c.put(2, y, f"{name:<8}", t.bright)
            c.put(11, y, f"{hi}°" if hi is not None else "", t.bright)
            c.put(16, y, f"/ {lo}°" if lo is not None else "", t.fg)
            c.put(23, y, f"RAIN {pop:>2}%" if pop else "DRY", t.c("rain") if pop >= 30 else t.dim)
            c.put(34, y, main["shortForecast"].upper()[: w - 36], t.fg)


# --- rotating extras: discussion teletype / surf / coast-to-desert ---------------------

class ExtrasPanel(Panel):
    SLOTS = (("afd", 75), ("surf", 30), ("transect", 30))

    @classmethod
    def available(cls, app) -> list[tuple[str, int]]:
        srcs = ((getattr(app, "data", None) or {}).get("sources") or {})
        skip = {k for k in ("afd", "marine") if ((srcs.get(k) or {}).get("data") or {}).get("unsupported")}
        return [s for s in cls.SLOTS if not (s[0] == "afd" and "afd" in skip or s[0] == "surf" and "marine" in skip)]

    def current(self) -> tuple[str, float, int, int]:
        app = self.app
        slots = self.available(app)
        i = getattr(app, "extras_idx", 0) % len(slots)
        return slots[i][0], time.time() - getattr(app, "extras_since", time.time()), i, len(slots)

    def draw(self, c, t, w, h):
        kind, el, i, n = self.current()
        dots = "".join("●" if k == i else "○" for k in range(n))
        getattr(self, f"_{kind}")(c, t, w, h, el, dots)

    def _afd(self, c, t, w, h, el, dots):
        afd = self.src("afd") or {}
        c.box(0, 0, w, h, t.dim, f"FORECAST DISCUSSION · NWS {afd.get('office', '')}", t.bright,
              right=f"ISSUED {hm(ptime(afd.get('issued')))}  {dots}")
        sec = afd.get("sections") or {}
        if not sec:
            c.put(3, 2, "WAITING FOR DISCUSSION…", t.dim)
            return
        order = ["SYNOPSIS", "SHORT TERM", "DISCUSSION", "LONG TERM", "MARINE"]
        keys = [k for k in order if any(s.startswith(k) for s in sec)]
        text = "   ".join(f".{k}... " + next(v for s, v in sec.items() if s.startswith(k)) for k in keys[:2])
        n = int(el * 45)
        typed = text[:n].upper()
        lines = textwrap.wrap(typed, w - 6) or [""]
        lines = lines[-(h - 2):]
        for r, line in enumerate(lines):
            c.put(3, 1 + r, line, t.fg)
        if n < len(text) and int(time.time() * 2) % 2 == 0:
            c.put(3 + len(lines[-1]), len(lines), "█", t.bright)

    def _surf(self, c, t, w, h, el, dots):
        m = self.src("marine") or {}
        cur = m.get("current") or {}
        wt = self.src("water_temp") or {}
        c.box(0, 0, w, h, t.dim, f"SURF · {self.label('surf_name')} · OPEN-METEO MARINE", t.bright, right=dots)
        alerts = (self.src("alerts") or {}).get("alerts") or []
        lines = [
            (f"WAVES  {fmt(cur.get('wave_height'), 1, ' FT')}  @ {fmt(cur.get('wave_period'), 0, ' S')}"
             f"  FROM {glyphs.compass_name(cur.get('wave_direction'))}", t.bright),
            (f"SWELL  {fmt(cur.get('swell_wave_height'), 1, ' FT')}  @ {fmt(cur.get('swell_wave_period'), 0, ' S')}"
             f"  FROM {glyphs.compass_name(cur.get('swell_wave_direction'))}", t.fg),
            (f"SEA SURFACE {fmt(cur.get('sea_surface_temperature'), 0, '°F')} MODEL   "
             f"{self.label('water_name')} SENSOR {fmt(wt.get('temp_f'), 1, '°F')}", t.fg),
        ]
        for al in alerts:
            if "surf" in (al.get("event") or "").lower() or "rip" in (al.get("event") or "").lower():
                lines.append(("▲ " + al["event"].upper() + " IN EFFECT", t.warn))
        for r, (s, st) in enumerate(lines[: h - 3]):
            c.put(3, 1 + r, s, st)
        off = int(el * 4)
        wave = "_.~^~._.-~^~-."
        c.put(3, h - 2, "".join(wave[(k + off) % len(wave)] for k in range(min(56, w - 32))), t.c("water"))
        # wave height chart on the right
        hr = m.get("hourly") or {}
        times = [ptime(x) for x in hr.get("time") or []]
        wh = hr.get("wave_height") or []
        if h >= 12:   # tall panel: chart under the text, full width
            x0, ytop = 3, 1 + len(lines) + 2
        else:
            x0, ytop = 64, 1
        if not times or w - x0 < 30 or h - ytop < 5:
            return
        now = datetime.now(TZ)
        start, end = now - timedelta(hours=12), now + timedelta(hours=48)
        pts = [(d, v) for d, v in zip(times, wh) if v is not None and start <= d <= end]
        if len(pts) < 2:
            return
        vmax = max(v for _, v in pts) * 1.15 + 0.1
        pw, ph = w - x0 - 8, h - ytop - 3
        X = lambda d: x0 * 2 + (d - start).total_seconds() / (end - start).total_seconds() * (pw * 2 - 1)  # noqa
        Y = lambda v: ytop * 4 + (vmax - v) / vmax * (ph * 4 - 1)  # noqa
        c.polyline([(X(d), Y(v)) for d, v in pts], t.c("water"), 3)
        nx = int(X(now))
        for yy in range(ytop * 4, (ytop + ph) * 4, 2):
            c.dot(nx, yy, t.accent, 4)
        top = max(pts, key=lambda p: p[1])
        c.put(w - 7, ytop, f"{vmax:.0f}FT", t.dim)
        c.put(min(int(X(top[0])) // 2, w - 24), max(ytop, int(Y(top[1])) // 4 - 1),
              f"PEAK {top[1]:.1f}' {top[0].strftime('%a').upper()} {hour_label(top[0])}", t.bright)
        c.put(w - 26, h - 2, "WAVE HEIGHT -12H … +48H", t.dim)

    def _transect(self, c, t, w, h, el, dots):
        tr = self.src("transect") or {}
        pts = tr.get("points") or []
        c.box(0, 0, w, h, t.dim, f"{self.label('transect_title', 'TRANSECT')} · TEMPERATURE NOW", t.bright, right=dots)
        if not pts:
            c.put(3, 2, "WAITING FOR DATA…", t.dim)
            return
        n = len(pts)
        x0, x1 = 12, w - 9
        xs = [x0 + (x1 - x0) * i / (n - 1) for i in range(n)]
        emax = max(p["elevation_ft"] for p in pts) * 1.1 + 100
        top, bot = 2, h - 4  # rows for terrain
        T, B = top * 4, bot * 4 + 3

        def Y(e):
            return B - e / emax * (B - T)

        prof = []
        for i in range(n - 1):
            a, b = pts[i], pts[i + 1]
            for k in range(int((xs[i + 1] - xs[i]) * 2)):
                f = k / ((xs[i + 1] - xs[i]) * 2)
                f2 = (1 - math.cos(math.pi * f)) / 2
                e = a["elevation_ft"] + (b["elevation_ft"] - a["elevation_ft"]) * f2
                e += math.sin(k * 0.9 + i) * emax * 0.012  # a little ridge texture
                prof.append((xs[i] * 2 + k, Y(max(e, 0))))
        for x, y in prof:
            x = int(x)
            for fy in range(int(y) + 1, B + 1):
                if BAYER[fy & 3][x & 1] < 2:
                    c.dot(x, fy, t.c("faint"), 1)
        c.polyline(prof, t.c("land"), 3)
        c.polyline([(x, y + 1) for x, y in prof], t.c("land"), 3)  # double ridge line reads better
        for k in range(2, x0 * 2 - 2):  # ocean
            if (k + int(el * 3)) % 6 < 3:
                c.dot(k, B - 1, t.c("water"), 2)
        c.put(2, bot - 1, self.label("transect_water")[: x0 - 2], t.c("water"))
        temps = [p["temp_f"] for p in pts if p["temp_f"] is not None]
        tlo, thi = (min(temps), max(temps)) if temps else (0, 1)
        for p, x in zip(pts, xs):
            cx = int(x)
            cy = int(Y(p["elevation_ft"])) // 4
            lab = f"{p['temp_f']:.0f}°"
            st = t.bright
            if t.name == "COLOR" and temps:
                f = (p["temp_f"] - tlo) / max(thi - tlo, 1)
                st = "#5fafff" if f < 0.33 else "#ffd75f" if f < 0.66 else "#ff875f"
            c.put(cx - 1, max(1, cy - 1), lab, st)
            c.put(cx - len(p["name"]) // 2, h - 3, p["name"], t.fg)
            el_ = f"{p['elevation_ft']:,}'"
            c.put(cx - len(el_) // 2, h - 2, el_, t.dim)
            if cy - 2 >= 1:
                c.put(cx - 4, cy - 2, f"H{p['hi']:.0f} L{p['lo']:.0f}", t.dim)


# --- status line ------------------------------------------------------------------------

class StatusBar(Panel):
    WATCH = (("obs", "NWS OBS"), ("forecast", "FORECAST"), ("openmeteo", "OPEN-METEO"), ("radar", "RADAR"),
             ("tides", "TIDES"), ("marine", "MARINE"), ("afd", "DISCUSSION"))

    def draw(self, c, t, w, h):
        app = self.app
        x = 1
        c.put(x, 0, "WXTERM", t.bright)
        x += 8
        err = getattr(app, "api_error", None)
        if err:
            c.put(x, 0, f"COLLECTOR UNREACHABLE: {err}"[: w - x - 40], t.warn)
        else:
            srcs = ((getattr(app, "data", None) or {}).get("sources") or {}) | \
                {"radar": (getattr(app, "data", None) or {}).get("radar_meta") or {}}
            for key, label in self.WATCH:
                ent = srcs.get(key) or {}
                if (ent.get("data") or {}).get("unsupported"):
                    continue
                bad = self.stale(key) if key != "radar" else not ent.get("ok_at")
                s = f"{'○' if bad or ent.get('error') else '●'} {label} {ago(ent.get('ok_at'))}"
                c.put(x, 0, s, t.warn if bad or ent.get("error") else t.dim)
                x += len(s) + 3
        right = (f"{app.loc.upper()} [L]  THEME {app.pal.name} [C]  GIF [G]  SHUFFLE [S]  PAUSE [P]  REFRESH [R]"
                 "  QUIT [Q]")
        rx = w - len(right) - 1
        msg = getattr(app, "gif_msg", "")
        if msg:
            c.put(rx - len(msg) - 3, 0, f" {msg} ", t.accent)
        c.put(rx, 0, right, t.dim)
