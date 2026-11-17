"""wxterm: full-screen terminal weather display. Talks to one wxterm collector per location (see api.locations()).

The big map shows either the weather radar loop or live air traffic (`a`, see flights.py)."""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical

from . import api, gif, glyphs, remote
from .flights import RANGES, TAG_MODES, AirPanel, Traffic
from .panels import set_tz, DaysPanel, ExtrasPanel, GraphPanel, NowPanel, RadarPanel, StatusBar, TidePanel
from .themes import THEMES, by_name

# Saved theme + location. The kiosk points WXTERM_STATE at /var/lib/wxterm/state.json so scripts/wxctl.sh
# (run by another user over SSH) can switch the screen; the app watches the file for changes.
STATE = Path(os.environ.get("WXTERM_STATE") or
             Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wxterm" / "state.json")


class WxApp(App):
    CSS = """
    Screen { layout: vertical; overflow: hidden; }
    #mid { height: 1fr; }
    #map { width: 1fr; }
    #radar, #air { width: 1fr; height: 1fr; }
    #right { width: 88; }
    #now { height: 12; }
    #graph { height: 15; }
    #tide { height: 11; }
    #days { height: 7; }
    #extras { height: 1fr; }
    #status { height: 1; }
    """
    BINDINGS = [("c", "cycle_theme", "Theme"), ("l", "cycle_location", "Location"), ("p", "pause", "Pause radar"),
                ("r", "refresh", "Refresh"), ("q", "quit", "Quit"), ("g", "gif", "Save GIF"),
                ("f", "cycle_font", "Big font"), ("s", "shuffle", "Shuffle layout"),
                # the AIR map (air traffic); these do nothing on the radar map
                ("a", "toggle_air", "Air traffic"), ("plus,equals_sign", "air_zoom(-1)", "Zoom in"),
                ("minus,underscore", "air_zoom(1)", "Zoom out"), ("t", "air_tags", "Data tags"),
                ("w", "air_wx", "Radar under traffic"),
                Binding("tab", "air_next(1)", show=False, priority=True),
                Binding("shift+tab", "air_next(-1)", show=False, priority=True),
                Binding("escape", "air_select_none", show=False, priority=True)]
    # Burn-in guard: every SHUFFLE_MIN minutes swap the map side, reorder the sidebar panels and move the status bar,
    # so no border or label sits on the same pixels for hours. 0 turns it off.
    SHUFFLE_MIN = float(os.environ.get("WXTERM_SHUFFLE_MIN", "15"))
    SIDEBAR = ("#now", "#graph", "#tide", "#days", "#extras")

    def __init__(self):
        super().__init__()
        st = self._load_state()
        # saved choices win; WXTERM_THEME / WXTERM_BIGFONT (kiosk.conf) are only the first-run defaults
        self.theme_idx = by_name(st.get("theme") or os.environ.get("WXTERM_THEME"))
        self.locs = api.locations()
        names = [n for n, _ in self.locs]
        # Always start on WXTERM_START (home): other cities are a manual toggle and never stick across restarts
        want = os.environ.get("WXTERM_LOCATION") or os.environ.get("WXTERM_START", "home")
        self.loc_idx = names.index(want) if want in names else 0
        bf = st.get("bigfont") or os.environ.get("WXTERM_BIGFONT")
        self.bigfont = bf if bf in glyphs.STYLES else glyphs.STYLES[0]
        self._gif_token = st.get("gif")          # scripts/wxctl.sh gif bumps this to record remotely
        self._recording = False
        self.gif_msg = ""
        self._gif_msg_until = 0.0
        self._state_mtime = self._mtime()
        self.data: dict | None = None
        self.radar: dict | None = None
        self.geo: dict | None = None
        self.api_error: str | None = None
        self.radar_idx = 0
        self.radar_hold = 0
        self.radar_paused = False
        self.extras_idx = 0
        self.extras_since = time.time()
        self._radar_want: tuple[int, int] | None = None
        self._started = time.time()
        # AIR map: saved choices win, WXTERM_MAP (kiosk.conf MAP) is the first-run default
        mode = st.get("map") or os.environ.get("WXTERM_MAP", "radar")
        self.map_mode = "air" if mode == "air" else "radar"
        self.air_range = st.get("air_range") if st.get("air_range") in RANGES else 25
        self.air_tags = st.get("air_tags") if st.get("air_tags") in TAG_MODES else "AUTO"
        self.air_wx = st.get("air_wx", True) is not False   # latest radar frame under the traffic
        self.traffic = Traffic()
        self.air_wxgrid: dict | None = None
        self._air_wx_want: tuple | None = None
        self._air_busy = self._routes_busy = False

    @property
    def pal(self):
        return THEMES[self.theme_idx]

    @property
    def loc(self) -> str:
        return self.locs[self.loc_idx][0]

    @property
    def base(self) -> str:
        return self.locs[self.loc_idx][1]

    # --- layout ---
    def compose(self) -> ComposeResult:
        with Horizontal(id="mid"):
            with Container(id="map"):
                yield RadarPanel(id="radar")
                yield AirPanel(id="air")
            with Vertical(id="right"):
                yield NowPanel(id="now")
                yield GraphPanel(id="graph")
                yield TidePanel(id="tide")
                yield DaysPanel(id="days")
                yield ExtrasPanel(id="extras")
        yield StatusBar(id="status")

    def on_mount(self):
        self._show_map()
        self._apply_theme()
        self._save_state()  # record the start location so wxctl.sh and the screen agree
        self.action_refresh()
        self.set_interval(30, self._fetch_all_bg)
        self.set_interval(120, self.request_radar)
        # retry fast until frames arrive: right after boot a collector can answer with an empty loop
        self.set_interval(10, lambda: not (self.radar or {}).get("frames") and self.request_radar())
        self.set_interval(1, self._tick)
        self.set_interval(0.35, self._radar_tick)
        self.set_interval(0.15, self._extras_tick)
        self.set_interval(2, self._watch_state)
        self.set_interval(10, self._write_status)
        self.set_interval(2.5, self._air_poll)
        self.set_interval(2, self._air_routes)
        self.set_interval(0.5, self._air_tick)
        self.set_interval(300, self.request_air_wx)
        if self.SHUFFLE_MIN > 0:
            self.set_interval(self.SHUFFLE_MIN * 60, self.action_shuffle)

    # --- state ---
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE.read_text())
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _mtime() -> float:
        try:
            return STATE.stat().st_mtime
        except OSError:
            return 0.0

    def _save_state(self):
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"theme": self.pal.name, "location": self.loc, "bigfont": self.bigfont,
                                       "gif": self._gif_token, "map": self.map_mode, "air_range": self.air_range,
                                       "air_tags": self.air_tags, "air_wx": self.air_wx}))
            tmp.replace(STATE)
            self._state_mtime = self._mtime()
        except OSError:
            pass

    def _write_status(self):
        """Publish what the screen is doing for remote readers (the MCP server's get_status)."""
        mid, right = self.query_one("#mid"), self.query_one("#right")
        frames = (self.radar or {}).get("frames") or []
        tr = self.traffic
        remote.write_status(STATE, {
            "time": time.time(), "started": self._started, "pid": os.getpid(),
            "cells": [self.size.width, self.size.height],
            "location": self.loc, "theme": self.pal.name, "bigfont": self.bigfont,
            "radar_paused": self.radar_paused, "radar_frames": len(frames), "recording_gif": self._recording,
            "map_view": self.map_mode,
            "air": {"range_nm": self.air_range, "tags": self.air_tags, "radar_under": self.air_wx,
                    "aircraft": len(tr.planes), "feed": tr.source or None, "feed_error": tr.error or None,
                    "feed_age_s": round(time.time() - tr.ok_at) if tr.ok_at else None,
                    "selected": tr.planes[tr.selected].callsign if tr.selected in tr.planes else None},
            "layout": {"map": "left" if mid.children[0].id == "map" else "right",
                       "sidebar": [w.id for w in right.children],
                       "status_bar": "top" if self.screen.children[0].id == "status" else "bottom"},
            "data_error": self.api_error, "shuffle_min": self.SHUFFLE_MIN,
        })

    def _watch_state(self):
        """Pick up theme/location changes written by scripts/wxctl.sh, and screenshot requests (remote.py)."""
        for shot_id, scale in remote.pending_shots(STATE):
            self.run_worker(lambda i=shot_id, sc=scale: remote.take_shot(STATE, i, sc), thread=True, group="shot")
        m = self._mtime()
        if m == self._state_mtime:
            return
        self._state_mtime = m
        st = self._load_state()
        if st.get("theme") and by_name(st["theme"]) != self.theme_idx:
            self.theme_idx = by_name(st["theme"])
            self._apply_theme()
        names = [n for n, _ in self.locs]
        if st.get("location") in names and names.index(st["location"]) != self.loc_idx:
            self._switch_location(names.index(st["location"]), save=False)
        if st.get("bigfont") in glyphs.STYLES and st["bigfont"] != self.bigfont:
            self.bigfont = st["bigfont"]
            self.query_one("#now").refresh()
        if st.get("map") in ("air", "radar") and st["map"] != self.map_mode:
            self._set_map(st["map"], save=False)
        if st.get("gif") and st.get("gif") != self._gif_token:
            self._gif_token = st["gif"]
            self.action_gif()

    def _apply_theme(self):
        self.screen.styles.background = self.pal.bg
        for w in self.query("#mid, #map, #right, #now, #radar, #air, #graph, #tide, #days, #extras, #status"):
            w.styles.background = self.pal.bg
        self.refresh_all()

    def refresh_all(self):
        for w in self.query("#now, #radar, #air, #graph, #tide, #days, #extras, #status"):
            w.refresh()

    # --- actions ---
    def action_cycle_theme(self):
        self.theme_idx = (self.theme_idx + 1) % len(THEMES)
        self._save_state()
        self._apply_theme()

    def layout_signature(self) -> tuple:
        mid, right = self.query_one("#mid"), self.query_one("#right")
        return (mid.children[0].id, tuple(w.id for w in right.children),
                self.screen.children[0].id)

    def action_shuffle(self):
        """Move things around a little (map side, sidebar order, status bar top/bottom). Skipped while recording."""
        if self._recording:
            return
        mid, right = self.query_one("#mid"), self.query_one("#right")
        before = self.layout_signature()
        for _ in range(10):
            order = list(self.SIDEBAR)
            random.shuffle(order)
            map_left, status_top = random.random() < 0.5, random.random() < 0.5
            sig = ("map" if map_left else "right", tuple(o[1:] for o in order), "status" if status_top else "mid")
            if sig != before:
                break
        widgets = [self.query_one(sel) for sel in order]
        for i, w in enumerate(widgets):
            if right.children[i] is not w:
                right.move_child(w, before=i)
        mp = self.query_one("#map")
        if map_left and mid.children[0] is not mp:
            mid.move_child(mp, before=0)
        elif not map_left and mid.children[0] is mp:
            mid.move_child(mp, after=right)
        status = self.query_one("#status")
        if status_top and self.screen.children[0] is not status:
            self.screen.move_child(status, before=0)
        elif not status_top and self.screen.children[0] is status:
            self.screen.move_child(status, after=mid)
        self.refresh_all()

    def action_cycle_font(self):
        self.bigfont = glyphs.STYLES[(glyphs.STYLES.index(self.bigfont) + 1) % len(glyphs.STYLES)]
        self._save_state()
        self.query_one("#now").refresh()

    def flash(self, msg: str, secs: float = 8):
        self.gif_msg, self._gif_msg_until = msg, time.time() + secs
        self.query_one("#status").refresh()

    def action_gif(self):
        """Record one full radar loop of the screen to a GIF in WXTERM_GIF_DIR (~/gifs)."""
        if self._recording:
            return
        why = gif.available()
        if why:
            self.flash(why)
            return
        frames = len((self.radar or {}).get("frames") or []) or 13
        seconds = (frames + 8) * 0.35 + 0.4   # every frame plus the hold on the latest = one clean loop
        if self.map_mode == "air":
            seconds = 10.0                    # no loop to follow: ten seconds of traffic
        self.radar_idx, self.radar_hold, self.radar_paused = 0, 0, False
        self._recording = True
        self.gif_msg = ""  # keep the status bar clean while recording
        self.query_one("#status").refresh()
        name = self.loc + ("-air" if self.map_mode == "air" else "")

        def work():
            try:
                path = gif.record(seconds, name, status=lambda m: self.call_from_thread(self.flash, m, 600))
                shown = str(path).replace(str(gif.GIF_DIR), "~/gifs")
                self.call_from_thread(self.flash, f"SAVED {shown} ({path.stat().st_size // 1024} KB)", 10)
            except Exception as e:  # noqa: BLE001
                self.call_from_thread(self.flash, f"GIF FAILED: {e}"[:60], 12)
            finally:
                self._recording = False

        self.run_worker(work, thread=True, group="gif")

    def action_cycle_location(self):
        self._switch_location((self.loc_idx + 1) % len(self.locs))

    def _switch_location(self, idx: int, save: bool = True):
        self.loc_idx = idx
        base = self.base
        self.run_worker(lambda: api.get(base, "/api/refresh", timeout=5), thread=True, group="wake")
        self.data = self.radar = self.geo = None
        self.api_error = None
        self.radar_idx = 0
        self.traffic.reset()
        self.air_wxgrid = None
        if save:
            self._save_state()
        self.refresh_all()
        self.action_refresh()

    def action_pause(self):
        self.radar_paused = not self.radar_paused
        self.query_one("#radar").refresh()

    def action_refresh(self):
        self._fetch_all_bg()
        self.request_radar()
        self._air_poll()
        self.request_air_wx()

    # --- AIR map ---
    def _show_map(self):
        air = self.map_mode == "air"
        self.query_one("#radar").display = not air
        self.query_one("#air").display = air

    def action_toggle_air(self):
        self._set_map("radar" if self.map_mode == "air" else "air")

    def _set_map(self, mode: str, save: bool = True):
        self.map_mode = mode
        self._show_map()
        if save:
            self._save_state()
        # sizes settle after the next layout, so ask for data then
        if mode == "air":
            self.call_after_refresh(self._air_poll)
            self.call_after_refresh(self.request_air_wx)
        else:
            self.call_after_refresh(self.request_radar)
        self.refresh_all()

    def action_air_zoom(self, step: int):
        if self.map_mode != "air":
            return
        i = RANGES.index(self.air_range) if self.air_range in RANGES else 3
        rng = RANGES[max(0, min(len(RANGES) - 1, i + step))]
        if rng != self.air_range:
            self.air_range = rng
            self.traffic.placed.clear()
            self._save_state()
            self._air_poll()
            self.request_air_wx()
            self.query_one("#air").refresh()

    def action_air_tags(self):
        if self.map_mode != "air":
            return
        self.air_tags = TAG_MODES[(TAG_MODES.index(self.air_tags) + 1) % len(TAG_MODES)]
        self._save_state()
        self.query_one("#air").refresh()

    def action_air_wx(self):
        if self.map_mode != "air":
            return
        self.air_wx = not self.air_wx
        self._save_state()
        self.request_air_wx()
        self.query_one("#air").refresh()

    def action_air_next(self, step: int):
        """Tab / shift+Tab: select the next aircraft in the traffic list (nearest first)."""
        if self.map_mode != "air":
            return
        order = self.query_one(AirPanel).order
        if not order:
            return
        sel = self.traffic.selected
        i = (order.index(sel) + step) % len(order) if sel in order else (0 if step > 0 else -1)
        self.traffic.selected = order[i]
        self.query_one("#air").refresh()

    def check_action(self, action: str, parameters) -> bool | None:
        """AIR map keys only act on the AIR map; elsewhere (radar map, dialogs) Tab and Escape keep their usual jobs."""
        if action.startswith("air_") or action == "toggle_air":
            if len(self.screen_stack) > 1:
                return False
            if action.startswith("air_") and self.map_mode != "air":
                return False
            if action == "air_select_none" and not self.traffic.selected:
                return False
        return True

    def action_air_select_none(self):
        self.air_select(None)

    def air_select(self, hex_: str | None):
        """Select an aircraft (a click on it); clicking the selected one again clears it."""
        self.traffic.selected = hex_ if hex_ != self.traffic.selected else None
        self.query_one("#air").refresh()

    def _air_tick(self):
        if self.map_mode == "air":
            self.query_one("#air").refresh()

    def _air_poll(self):
        if self.map_mode != "air" or self._air_busy:
            return
        panel = self.query_one(AirPanel)
        if panel.size.width < 10:
            return
        base, dist = self.base, panel.fetch_dist()
        self._air_busy = True
        self.run_worker(lambda: self._fetch_air(base, dist), thread=True, group="air")

    def _fetch_air(self, base, dist):
        try:
            j = api.get(base, f"/api/aircraft?dist={dist}", timeout=12)
            self.call_from_thread(self._set_air, base, j, None)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            try:
                msg = json.loads(e.read()).get("error") or msg  # type: ignore[attr-defined]  # the collector's reason
            except Exception:  # noqa: BLE001
                pass
            self.call_from_thread(self._set_air, base, None, msg.upper()[:70])
        finally:
            self._air_busy = False

    def _set_air(self, base, j, err):
        if base != self.base:
            return
        if j is not None:
            self.traffic.ingest(j, time.time())
        else:
            self.traffic.error = err
        self.query_one("#air").refresh()

    def _air_routes(self):
        """Look up routes for the selected aircraft and the 30 nearest airborne ones, a few at a time."""
        if self.map_mode != "air" or self._routes_busy:
            return
        tr = self.traffic
        near = sorted((p for p in tr.planes.values() if not p.ground and hasattr(p, "d_home")),
                      key=lambda p: p.d_home)[:30]
        want = tr.wanted_routes(([tr.planes[tr.selected]] if tr.selected in tr.planes else []) + near)
        if not want:
            return
        for cs in want:
            tr.routes[cs] = "pending"
        base = self.base
        self._routes_busy = True
        self.run_worker(lambda: self._fetch_routes(base, want), thread=True, group="routes")

    def _fetch_routes(self, base, want):
        try:
            res = api.get(base, "/api/routes?cs=" + ",".join(want), timeout=60)
        except Exception:  # noqa: BLE001 - asked again on the next round
            res = None
        finally:
            self._routes_busy = False
        self.call_from_thread(self._set_routes, base, want, res)

    def _set_routes(self, base, want, res):
        if base != self.base:
            return
        for cs in want:
            if res is None:
                self.traffic.routes.pop(cs, None)
            else:
                self.traffic.routes[cs] = res.get(cs)

    def request_air_wx(self):
        """The latest radar frame, sampled to the AIR map's box, for the precipitation under the traffic."""
        if self.map_mode != "air" or not self.air_wx or not self.geo:
            return
        panel = self.query_one(AirPanel)
        if panel.size.width < 10:
            return
        w, h, _ = panel.view()
        bbox = panel.bbox(self.geo["home"])
        want = (w, h, self.air_range, self.base)
        self._air_wx_want = want
        q = f"/api/radar?w={w}&h={h}&last=1&bbox=" + ",".join(f"{v:.5f}" for v in bbox)
        self.run_worker(lambda: self._fetch_air_wx(want, q), thread=True, group="airwx")

    def _fetch_air_wx(self, want, q):
        try:
            r = api.get(want[3], q, timeout=60)
        except Exception:  # noqa: BLE001 - the traffic still shows; tried again in 5 minutes or on zoom
            return
        self.call_from_thread(self._set_air_wx, want, r)

    def _set_air_wx(self, want, r):
        if want == self._air_wx_want:
            self.air_wxgrid = r
            self.query_one("#air").refresh()

    # --- network (threads) ---
    def _fetch_all_bg(self):
        self.run_worker(self._fetch_all, thread=True, group="all", exclusive=True)
        if self.geo is None:  # first try can race the collectors at boot; keep retrying until it lands
            self.run_worker(self._fetch_geo, thread=True, group="geo", exclusive=True)

    def _fetch_all(self):
        base = self.base
        try:
            d = api.get(base, "/api/all")
            self.call_from_thread(self._set_data, base, d, None)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._set_data, base, None, f"{self.loc.upper()} {type(e).__name__}: {e}"[:80])

    def _set_data(self, base, d, err):
        if base != self.base:
            return
        if d is not None:
            set_tz((d.get("home") or {}).get("tz"))
            self.data = d
        self.api_error = err
        self.refresh_all()

    def _fetch_geo(self):
        base = self.base
        try:
            g = api.get(base, "/api/geo")
            self.call_from_thread(self._set_geo, base, g)
        except Exception:  # noqa: BLE001 - retried on next refresh
            pass

    def _set_geo(self, base, g):
        if base == self.base:
            self.geo = g
            self.request_air_wx()

    def request_radar(self):
        try:
            panel = self.query_one(RadarPanel)
            w, h = panel.dot_size()
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        if not panel.display:  # the AIR map is up; asked again when the radar map comes back
            return
        self._radar_want = (w, h)
        base = self.base
        self.run_worker(lambda: self._fetch_radar(base, w, h), thread=True, group="radar", exclusive=True)

    def _fetch_radar(self, base, w, h):
        try:
            r = api.get(base, f"/api/radar?w={w}&h={h}", timeout=60)
            self.call_from_thread(self._set_radar, base, r)
        except Exception:  # noqa: BLE001
            pass

    def _set_radar(self, base, r):
        if base != self.base:
            return
        if self._radar_want and (r.get("w"), r.get("h")) != self._radar_want:
            return  # stale size
        new_latest = (r.get("frames") or [{}])[-1].get("t")
        old_latest = ((self.radar or {}).get("frames") or [{}])[-1].get("t")
        self.radar = r
        if new_latest != old_latest:
            self.radar_idx = 0
        self.query_one("#radar").refresh()

    # --- timers ---
    def _tick(self):
        if self.gif_msg and time.time() > self._gif_msg_until:
            self.gif_msg = ""
        self.query_one("#now").refresh()
        self.query_one("#status").refresh()
        if int(time.time()) % 60 == 0:
            for sel in ("#graph", "#tide"):
                self.query_one(sel).refresh()

    def _radar_tick(self):
        frames = (self.radar or {}).get("frames") or []
        if not frames or self.radar_paused:
            return
        if self.radar_idx >= len(frames) - 1:
            self.radar_hold += 1
            if self.radar_hold < 8:  # linger on the latest frame
                return
            self.radar_hold, self.radar_idx = 0, 0
        else:
            self.radar_idx += 1
        self.query_one("#radar").refresh()

    def _extras_tick(self):
        slots = ExtrasPanel.available(self)
        if time.time() - self.extras_since > slots[self.extras_idx % len(slots)][1]:
            self.extras_idx = (self.extras_idx + 1) % len(slots)
            self.extras_since = time.time()
        self.query_one("#extras").refresh()


def main():
    WxApp().run()


if __name__ == "__main__":
    main()
