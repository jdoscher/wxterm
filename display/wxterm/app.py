"""wxterm: full-screen terminal weather display. Talks to one wxterm collector per location (see api.locations())."""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical

from . import api, gif, glyphs, remote
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
    #radar { width: 1fr; }
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
                ("f", "cycle_font", "Big font"), ("s", "shuffle", "Shuffle layout")]
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
        self._radar_want: tuple[int, int] | None = None
        self._started = time.time()

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
            yield RadarPanel(id="radar")
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
        self.set_interval(10, lambda: not (self.radar or {}).get("frames") and self.request_radar())
        self.set_interval(1, self._tick)
        self.set_interval(self.RADAR_STEP, self._radar_tick)
        self.set_interval(self.TYPE_STEP, self._extras_tick)
        self.set_interval(2, self._watch_state)
        self.set_interval(10, self._write_status)
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
                                       "gif": self._gif_token}))
            tmp.replace(STATE)
            self._state_mtime = self._mtime()
        except OSError:
            pass

    def _write_status(self):
        """Publish what the screen is doing for remote readers (the MCP server's get_status)."""
        mid, right = self.query_one("#mid"), self.query_one("#right")
        frames = (self.radar or {}).get("frames") or []
        remote.write_status(STATE, {
            "time": time.time(), "started": self._started, "pid": os.getpid(),
            "cells": [self.size.width, self.size.height],
            "location": self.loc, "theme": self.pal.name, "bigfont": self.bigfont,
            "radar_paused": self.radar_paused, "radar_frames": len(frames), "recording_gif": self._recording,
            "layout": {"map": "left" if mid.children[0].id == "radar" else "right",
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
        if st.get("gif") and st.get("gif") != self._gif_token:
            self._gif_token = st["gif"]
            self.action_gif()

    def _apply_theme(self):
        self.screen.styles.background = self.pal.bg
        for w in self.query("#mid, #right, #now, #radar, #graph, #tide, #days, #extras, #status"):
            w.styles.background = self.pal.bg
        self.refresh_all()

    def refresh_all(self):
        for w in self.query("#now, #radar, #graph, #tide, #days, #extras, #status"):
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
            sig = ("radar" if map_left else "right", tuple(o[1:] for o in order), "status" if status_top else "mid")
            if sig != before:
                break
        widgets = [self.query_one(sel) for sel in order]
        for i, w in enumerate(widgets):
            if right.children[i] is not w:
                right.move_child(w, before=i)
        radar = self.query_one("#radar")
        if map_left and mid.children[0] is not radar:
            mid.move_child(radar, before=0)
        elif not map_left and mid.children[0] is radar:
            mid.move_child(radar, after=right)
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
        seconds = (frames + 8) * self.RADAR_STEP + 0.4   # every frame plus the hold on the latest = one clean loop
        self.radar_idx, self.radar_hold, self.radar_paused = 0, 0, False
        self._recording = True
        self.gif_msg = ""  # keep the status bar clean while recording
        self.query_one("#status").refresh()
        name = self.loc

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
            self.call_from_thread(lambda: setattr(self, "geo", g) if base == self.base else None)
        except Exception:  # noqa: BLE001 - retried on next refresh
            pass

    def request_radar(self):
        try:
            w, h = self.query_one(RadarPanel).dot_size()
        except Exception:  # noqa: BLE001 - not mounted yet
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
        panel = self.query_one(ExtrasPanel)
        if time.time() - self.extras_since > slots[self.extras_idx % len(slots)][1]:
            self.extras_idx = (self.extras_idx + 1) % len(slots)
            self.extras_since = time.time()
            panel.typing = True  # draw the new slot now
        if panel.typing:  # static slots (surf, transect, a finished discussion) only redraw when data changes
            panel.refresh()


def main():
    WxApp().run()


if __name__ == "__main__":
    main()
