"""wxterm: full-screen terminal weather display. Talks to one wxterm collector per location (see api.locations()).

The big map (MapPanel, flights.py) has three layers, each switched on and off on its own: the radar loop (`w`), live
air traffic (`a`), spacecraft overhead (`o`, space.py) and APRS stations (`x`, aprs.py, by Steve Miller)."""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical

from . import api, aprs, gif, glyphs, remote, space
from .flights import DEFAULT_RANGE, RANGES, TAG_MODES, MapPanel, Traffic
from .panels import set_tz, DaysPanel, ExtrasPanel, GraphPanel, NowPanel, StatusBar, TidePanel
from .themes import THEMES, by_name

# Saved theme + location. The kiosk points WXTERM_STATE at /var/lib/wxterm/state.json so scripts/wxctl.sh
# (run by another user over SSH) can switch the screen; the app watches the file for changes.
STATE = Path(os.environ.get("WXTERM_STATE") or
             Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wxterm" / "state.json")
ON = ("on", "yes", "true", "1")


class WxApp(App):
    CSS = """
    Screen { layout: vertical; overflow: hidden; }
    #mid { height: 1fr; }
    #map { width: 1fr; }
    #mapview { width: 1fr; height: 1fr; }
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
                # map layers and range
                ("a", "toggle_air", "Air traffic"), ("w", "toggle_radar", "Radar"), ("o", "toggle_space", "Spacecraft"),
                ("x", "cycle_aprs", "APRS"),
                ("plus,equals_sign", "zoom(-1)", "Zoom in"), ("minus,underscore", "zoom(1)", "Zoom out"),
                # air traffic only; these do nothing while it's off
                ("t", "air_tags", "Data tags"),
                Binding("tab", "air_next(1)", show=False, priority=True),
                Binding("shift+tab", "air_next(-1)", show=False, priority=True),
                Binding("escape", "air_select_none", show=False, priority=True)]
    # Burn-in guard: every SHUFFLE_MIN minutes swap the map side, reorder the sidebar panels and move the status bar,
    # so no border or label sits on the same pixels for hours. 0 turns it off.
    SHUFFLE_MIN = float(os.environ.get("WXTERM_SHUFFLE_MIN", "15"))
    SIDEBAR = ("#now", "#graph", "#tide", "#days", "#extras")
    # Animation speed: seconds per radar frame and per teletype step. Slower values save CPU on small boards
    # (kiosk.conf PROFILE=pi sets 0.5 and 0.3).
    RADAR_STEP = max(0.1, float(os.environ.get("WXTERM_RADAR_STEP", "0.35")))
    TYPE_STEP = max(0.05, float(os.environ.get("WXTERM_TYPE_STEP", "0.15")))

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
        self._radar_want: tuple | None = None
        self._started = time.time()
        self._last_tick = time.time()      # wall clock at the last 1 s tick, to notice a suspend
        self.resumed_at = 0.0              # when the machine last woke up (0 = not since start)
        # map layers: saved choices win; WXTERM_AIR (kiosk.conf AIR) is the first-run default for air traffic,
        # which is off unless asked for because it polls the ADS-B feeds every few seconds
        air = st.get("air")
        self.air_on = air if isinstance(air, bool) else os.environ.get("WXTERM_AIR", "off").lower() in ON
        self.radar_on = st.get("radar") is not False
        self.air_range = st.get("air_range") if st.get("air_range") in RANGES else DEFAULT_RANGE
        self.air_tags = st.get("air_tags") if st.get("air_tags") in TAG_MODES else "AUTO"
        self.traffic = Traffic()
        self._air_busy = self._routes_busy = False
        # spacecraft: on unless turned off; the orbits are computed here, so it costs no network while it runs
        sp = st.get("space")
        self.space_on = sp if isinstance(sp, bool) else os.environ.get("WXTERM_SPACE", "on").lower() in ON
        self.sky = space.Sky()
        self.space_next: tuple[str, float] | None = None   # next crewed station over the map: (name, unix time)
        self.space_passes: list[space.Pass] | None = None  # passes over the location (None = not worked out yet)
        self._space_for: tuple | None = None               # (location, bbox) the two above were worked out for
        self._space_busy = False
        # APRS (only where the collector runs it): saved choice wins, WXTERM_APRS_VIEW (kiosk.conf) is the default
        view = (st.get("aprs_view") or os.environ.get("WXTERM_APRS_VIEW") or "ALL").upper()
        self.aprs_view = view if view in aprs.VIEWS else "ALL"
        self.aprs: dict | None = None

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
                yield MapPanel(id="mapview")
            with Vertical(id="right"):
                yield NowPanel(id="now")
                yield GraphPanel(id="graph")
                yield TidePanel(id="tide")
                yield DaysPanel(id="days")
                yield ExtrasPanel(id="extras")
        yield StatusBar(id="status")

    def on_mount(self):
        self._apply_theme()
        self._save_state()  # record the start location so wxctl.sh and the screen agree
        self.action_refresh()
        self.set_interval(30, self._fetch_all_bg)
        self.set_interval(120, self.request_radar)
        # retry fast until frames arrive: right after boot a collector can answer with an empty loop
        self.set_interval(10, lambda: self.radar_on and not (self.radar or {}).get("frames") and self.request_radar())
        self.set_interval(1, self._tick)
        self.set_interval(self.RADAR_STEP, self._radar_tick)
        self.set_interval(self.TYPE_STEP, self._extras_tick)
        self.set_interval(2, self._watch_state)
        self.set_interval(10, self._write_status)
        self.set_interval(2.5, self._air_poll)
        self.set_interval(2, self._air_routes)
        self.set_interval(0.5, self._air_tick)
        self.set_interval(30, self._space_scan)
        self.set_interval(600, self._space_slow)
        self.set_interval(3 * 3600, self._space_fetch)
        self.set_interval(20, self._fetch_aprs_bg)
        self.call_after_refresh(self._space_fetch)
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
                                       "gif": self._gif_token, "air": self.air_on, "radar": self.radar_on,
                                       "space": self.space_on, "aprs_view": self.aprs_view,
                                       "air_range": self.air_range, "air_tags": self.air_tags}))
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
            "layers": {"radar": self.radar_on, "air": self.air_on, "space": self.space_on},
            "range_nm": self.air_range,
            "aprs": {"view": self.aprs_view, "stations": len(aprs.stations(self)),
                     "error": ((self.data or {}).get("sources", {}).get("aprs") or {}).get("error")}
            if aprs.enabled(self) else None,
            "space": {"objects": len(self.sky), "near_map": len(self.sky.cand),
                      "next_station_over_map": ({"name": self.space_next[0], "at": round(self.space_next[1])}
                                                if self.space_next else None)} if self.space_on else None,
            "air": {"tags": self.air_tags, "aircraft": len(tr.planes), "feed": tr.source or None,
                    "feed_error": tr.error or None,
                    "feed_age_s": round(time.time() - tr.ok_at) if tr.ok_at else None,
                    "selected": tr.planes[tr.selected].callsign if tr.selected in tr.planes else None}
            if self.air_on else None,
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
        if isinstance(st.get("air"), bool) and st["air"] != self.air_on:
            self._set_air_layer(st["air"], save=False)
        if isinstance(st.get("radar"), bool) and st["radar"] != self.radar_on:
            self._set_radar_layer(st["radar"], save=False)
        if st.get("aprs_view") in aprs.VIEWS and st["aprs_view"] != self.aprs_view:
            self._set_aprs_view(st["aprs_view"], save=False)
        if isinstance(st.get("space"), bool) and st["space"] != self.space_on:
            self._set_space_layer(st["space"], save=False)
        if st.get("air_range") in RANGES and st["air_range"] != self.air_range:
            self._set_range(st["air_range"], save=False)   # saving here would write back the other old values
        if st.get("air_tags") in TAG_MODES and st["air_tags"] != self.air_tags:
            self.air_tags = st["air_tags"]
            self._refresh_map()
        if st.get("gif") and st.get("gif") != self._gif_token:
            self._gif_token = st["gif"]
            self.action_gif()

    def _apply_theme(self):
        self.screen.styles.background = self.pal.bg
        for w in self.query("#mid, #map, #right, #now, #mapview, #graph, #tide, #days, #extras, #status"):
            w.styles.background = self.pal.bg
        self.refresh_all()

    def refresh_all(self):
        for w in self.query("#now, #mapview, #graph, #tide, #days, #extras, #status"):
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
        """Record one full radar loop of the screen to a GIF in WXTERM_GIF_DIR (~/gifs); 10 s with radar off."""
        if self._recording:
            return
        why = gif.available()
        if why:
            self.flash(why)
            return
        frames = len((self.radar or {}).get("frames") or []) or 13
        seconds = (frames + 8) * self.RADAR_STEP + 0.4   # every frame plus the hold on the latest = one clean loop
        if not self.radar_on:
            seconds = 10.0                    # no loop to follow: ten seconds of traffic
        self.radar_idx, self.radar_hold, self.radar_paused = 0, 0, False
        self._recording = True
        self.gif_msg = ""  # keep the status bar clean while recording
        self.query_one("#status").refresh()
        name = self.loc + ("-air" if self.air_on else "")

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
        self.sky.cand, self.space_next, self.space_passes, self._space_for = [], None, None, None
        self.aprs = None
        if save:
            self._save_state()
        self.refresh_all()
        self.action_refresh()

    def action_pause(self):
        self.radar_paused = not self.radar_paused
        self._refresh_map()

    def action_refresh(self):
        self._fetch_all_bg()
        self.request_radar()
        self._air_poll()

    def _refresh_map(self):
        """Redraw the map, if it's mounted (timers can fire while the app is shutting down)."""
        for w in self.query("#mapview"):
            w.refresh()

    # --- map layers and range ---
    def action_toggle_air(self):
        self._set_air_layer(not self.air_on)

    def _set_air_layer(self, on: bool, save: bool = True):
        self.air_on = on
        if not on:
            self.traffic.selected = None
        if save:
            self._save_state()
        # the traffic list beside the map comes and goes, so the map's size changes: ask for data after layout
        self.call_after_refresh(self._air_poll)
        self.call_after_refresh(self.request_radar)
        self.refresh_all()

    def action_toggle_space(self):
        self._set_space_layer(not self.space_on)

    def _set_space_layer(self, on: bool, save: bool = True):
        self.space_on = on
        if save:
            self._save_state()
        if on:
            self._space_scan()
            self._space_slow()
        self.refresh_all()

    def action_cycle_aprs(self):
        self._set_aprs_view(aprs.VIEWS[(aprs.VIEWS.index(self.aprs_view) + 1) % len(aprs.VIEWS)])

    def _set_aprs_view(self, view: str, save: bool = True):
        self.aprs_view = view
        if save:
            self._save_state()
        self._fetch_aprs_bg()
        self.refresh_all()

    def _fetch_aprs_bg(self):
        """Every 20 s while this location's collector runs APRS (even with the layer off: its panels read it)."""
        if not aprs.enabled(self):
            return
        base = self.base

        def work():
            try:
                j = api.get(base, "/api/aprs")
            except Exception:  # noqa: BLE001 - shown as stale in the status bar; asked again in 20 s
                return
            self.call_from_thread(self._set_aprs, base, j)

        self.run_worker(work, thread=True, group="aprs", exclusive=True)

    def _set_aprs(self, base, j):
        if base == self.base:
            self.aprs = j
            self._refresh_map()

    def action_toggle_radar(self):
        self._set_radar_layer(not self.radar_on)

    def _set_radar_layer(self, on: bool, save: bool = True):
        self.radar_on = on
        self.radar = None   # fetched fresh when it comes back on, so it's never an old loop
        self.radar_idx = self.radar_hold = 0
        if save:
            self._save_state()
        self.request_radar()
        self.refresh_all()

    def action_zoom(self, step: int):
        i = RANGES.index(self.air_range) if self.air_range in RANGES else RANGES.index(DEFAULT_RANGE)
        rng = RANGES[max(0, min(len(RANGES) - 1, i + step))]
        if rng != self.air_range:
            self._set_range(rng)

    def _set_range(self, rng: int, save: bool = True):
        self.air_range = rng
        self.traffic.placed.clear()
        self.radar = None   # sampled for the old range; the new loop arrives in a second or two
        if save:
            self._save_state()
        self._air_poll()
        self.request_radar()
        self.sky.cand = []
        self._space_scan()
        self._space_slow()
        self._refresh_map()

    def action_air_tags(self):
        if not self.air_on:
            return
        self.air_tags = TAG_MODES[(TAG_MODES.index(self.air_tags) + 1) % len(TAG_MODES)]
        self._save_state()
        self._refresh_map()

    def action_air_next(self, step: int):
        """Tab / shift+Tab: select the next aircraft in the traffic list (nearest first)."""
        if not self.air_on:
            return
        order = self.query_one(MapPanel).order
        if not order:
            return
        sel = self.traffic.selected
        i = (order.index(sel) + step) % len(order) if sel in order else (0 if step > 0 else -1)
        self.traffic.selected = order[i]
        self._refresh_map()

    def check_action(self, action: str, parameters) -> bool | None:
        """Map keys stay out of dialogs, and air traffic keys only act while it's on; otherwise Tab and Escape keep
        their usual jobs."""
        if action.startswith("air_") or action in ("toggle_air", "toggle_radar", "toggle_space", "cycle_aprs", "zoom"):
            if len(self.screen_stack) > 1:
                return False
            if action.startswith("air_") and not self.air_on:
                return False
            if action == "air_select_none" and not self.traffic.selected:
                return False
        return True

    def action_air_select_none(self):
        self.air_select(None)

    def air_select(self, hex_: str | None):
        """Select an aircraft (a click on it); clicking the selected one again clears it."""
        self.traffic.selected = hex_ if hex_ != self.traffic.selected else None
        self._refresh_map()

    def _moving(self) -> bool:
        """Something on the map moves by itself: aircraft, or a spacecraft on or near it."""
        return self.air_on or (self.space_on and bool(self.sky.cand))

    def _air_tick(self):
        if self._moving() and not self._radar_running():   # while the loop runs, _radar_tick moves things too
            self._refresh_map()

    def _air_poll(self):
        if not self.air_on or self._air_busy:
            return
        panel = self.query(MapPanel).first(MapPanel) if self.query(MapPanel) else None
        if panel is None or panel.size.width < 10:
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
        elif not self.waking():
            self.traffic.error = err
        self._refresh_map()

    def _air_routes(self):
        """Look up routes for the selected aircraft and the 30 nearest airborne ones, a few at a time."""
        if not self.air_on or self._routes_busy:
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
            first = self.data is None
            self.data = d
            if first:
                self._fetch_aprs_bg()
        if err and self.waking():
            return   # Wi-Fi is still reconnecting after a suspend; the retries above will get through
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
            self.request_radar()   # the radar box is centred on home, which comes with the map
            self.call_after_refresh(self._space_scan)
            self.call_after_refresh(self._space_slow)

    def request_radar(self):
        """The radar loop, sampled by the collector to the map's box at the current range."""
        if not self.radar_on or not self.geo:
            return
        panel = self.query(MapPanel).first(MapPanel) if self.query(MapPanel) else None
        if panel is None or panel.size.width < 10:
            return   # not laid out yet; on_resize asks again
        w, h, _ = panel.view()
        bbox = panel.bbox(self.geo["home"])
        want = (w, h, self.air_range, self.base)
        self._radar_want = want
        q = f"/api/radar?w={w}&h={h}&bbox=" + ",".join(f"{v:.5f}" for v in bbox)
        self.run_worker(lambda: self._fetch_radar(want, q), thread=True, group="radar", exclusive=True)

    def _fetch_radar(self, want, q):
        try:
            r = api.get(want[3], q, timeout=60)
            self.call_from_thread(self._set_radar, want, r)
        except Exception:  # noqa: BLE001 - asked again in 2 minutes, on zoom, or every 10 s while there's no loop
            pass

    def _set_radar(self, want, r):
        if want != self._radar_want or not self.radar_on:
            return  # made for another size, range or place
        new_latest = (r.get("frames") or [{}])[-1].get("t")
        old_latest = ((self.radar or {}).get("frames") or [{}])[-1].get("t")
        self.radar = r
        if new_latest != old_latest:
            self.radar_idx = 0
        self._refresh_map()

    # --- spacecraft (space.py) ---
    def _map_box(self):
        """(panel, bbox) of the map as it's laid out now, or (None, None) before the map and home are known."""
        panels = self.query(MapPanel)
        if not panels or not self.geo:
            return None, None
        panel = panels.first(MapPanel)
        if panel.size.width < 10:
            return None, None
        return panel, panel.bbox(self.geo["home"])

    def _space_fetch(self):
        """Orbital elements, a few times a day, always from the first location's collector (one download per group
        for the whole house; CelesTrak refuses repeats)."""
        bases = [self.locs[0][1]] + ([self.base] if self.base != self.locs[0][1] else [])

        def work():
            for g in space.GROUPS:
                for base in bases:
                    try:
                        j = api.get(base, f"/api/tle?group={g}", timeout=120)
                    except Exception:  # noqa: BLE001 - try the next collector, then wait for the next round
                        continue
                    if j.get("records") and j.get("fetched") != self.sky.loaded.get(g):
                        self.sky.load(g, j["records"], j["fetched"])
                        self.call_from_thread(self._space_scan)
                        self.call_from_thread(self._space_slow, True)
                    break
            # a group the collector hadn't fetched yet (CelesTrak spaces out the first downloads): ask again in
            # 10 minutes rather than at the next 3-hourly round
            if any(g not in self.sky.loaded for g in space.GROUPS):
                self.call_from_thread(self.set_timer, 600, self._space_fetch)

        self.run_worker(work, thread=True, group="tle", exclusive=True)

    def _space_scan(self):
        """Every 30 s: which spacecraft could be on the map in the next minute (the only ones drawn each frame)."""
        if not self.space_on or not len(self.sky) or self._space_busy:
            return
        _, bbox = self._map_box()
        if bbox is None:
            return
        self._space_busy = True
        want = (self.base, bbox)

        def work():
            try:
                cand = self.sky.candidates(bbox, time.time())
            finally:
                self._space_busy = False
            self.call_from_thread(self._set_space_cand, want, cand)

        self.run_worker(work, thread=True, group="space")

    def _set_space_cand(self, want, cand):
        if want == (self.base, self._map_box()[1]):
            self.sky.cand = cand
            self._refresh_map()

    def _space_slow(self, force: bool = False):
        """Every 10 minutes: the next crewed station over the map, and passes over the location for the panel."""
        if not self.space_on or not self.sky.stations:
            return
        _, bbox = self._map_box()
        if bbox is None:
            return
        want = (self.base, bbox)
        home = tuple(self.geo["home"])

        def work():
            now = time.time()
            nxt = self.sky.next_over_map(bbox, now, horizon=4 * 86400)
            passes = self.sky.passes(home, now)
            self.call_from_thread(self._set_space_slow, want, nxt, passes)

        self.run_worker(work, thread=True, group="space-slow", exclusive=True)

    def _set_space_slow(self, want, nxt, passes):
        if want[0] != self.base:
            return
        self.space_next, self._space_for = nxt, want
        if want[1] == self._map_box()[1] or self.space_passes is None:
            self.space_passes = passes
        self._refresh_map()

    # --- suspend and resume ---
    WAKE_GRACE = 60   # seconds after waking during which network errors are expected (Wi-Fi reconnecting)

    def _resumed(self, slept: float):
        """Back from a suspend: everything on screen is old. Drop the aircraft (they are dead-reckoned from their
        last fix, so after an hour they would be drawn far off course) and the old radar loop, then refresh."""
        self.resumed_at = time.time()
        self.traffic.reset()
        self.radar = None
        self.radar_idx = self.radar_hold = 0
        self.flash(f"AWAKE AFTER {fmt_span(slept)} · REFRESHING", 20)
        base = self.base
        self.run_worker(lambda: api.get(base, "/api/refresh", timeout=5), thread=True, group="wake")
        self.action_refresh()
        for delay in (10, 25, 45):   # the network may still be coming up: try again a few times
            self.set_timer(delay, self.action_refresh)
        self.refresh_all()

    def waking(self) -> bool:
        return bool(self.resumed_at) and time.time() - self.resumed_at < self.WAKE_GRACE

    # --- timers ---
    def _tick(self):
        now = time.time()
        if now - self._last_tick > 30:     # the machine slept (timers stop while it's suspended)
            self._resumed(now - self._last_tick)
        self._last_tick = now
        if self.gif_msg and time.time() > self._gif_msg_until:
            self.gif_msg = ""
        sel = "#now, #status" + (", #graph, #tide" if int(time.time()) % 60 == 0 else "")
        for w in self.query(sel):
            w.refresh()

    def _radar_running(self) -> bool:
        return self.radar_on and not self.radar_paused and bool((self.radar or {}).get("frames"))

    def _radar_tick(self):
        """Step the radar loop. With air traffic on too, this tick also moves the aircraft, so the map is drawn once
        per step instead of on two timers."""
        if not self._radar_running():
            return
        frames = self.radar["frames"]
        if self.radar_idx >= len(frames) - 1:
            self.radar_hold += 1
            if self.radar_hold < 8 and not self._moving():  # linger on the latest frame
                return
            if self.radar_hold >= 8:
                self.radar_hold, self.radar_idx = 0, 0
        else:
            self.radar_idx += 1
        self._refresh_map()

    def _extras_tick(self):
        panels = self.query(ExtrasPanel)
        if not panels:  # timers can fire while the app is shutting down and the panel is already gone
            return
        panel = panels.first(ExtrasPanel)
        slots = ExtrasPanel.available(self)
        if time.time() - self.extras_since > slots[self.extras_idx % len(slots)][1]:
            self.extras_idx = (self.extras_idx + 1) % len(slots)
            self.extras_since = time.time()
            panel.typing = True  # draw the new slot now
        if panel.typing:  # static slots (surf, transect, a finished discussion) only redraw when data changes
            panel.refresh()


def fmt_span(secs: float) -> str:
    m = int(secs // 60)
    return f"{m // 60}H {m % 60:02d}M" if m >= 60 else f"{max(m, 1)} MIN"


def main():
    WxApp().run()


if __name__ == "__main__":
    main()
