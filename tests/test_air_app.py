"""The AIR map inside the running app (display/wxterm/app.py), driven with Textual's test pilot against a fake
collector: which map shows at start, the keys, what gets saved, and what reaches the screen."""
from __future__ import annotations

import asyncio
import io
import json
import os
import time
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

from wxterm import api, flights as F
from wxterm import app as wxapp

HOME = (37.7749, -122.4194)
SFO = {"iata": "SFO", "icao": "KSFO", "name": "San Francisco Intl", "city": "San Francisco",
       "lat": 37.6198, "lon": -122.3748}
ORD = {"iata": "ORD", "icao": "KORD", "name": "O'Hare Intl", "city": "Chicago", "lat": 41.9786, "lon": -87.9048}


def _at(brg, nm, **kw) -> dict:
    lat, lon = F.dest(*HOME, brg, nm)
    return {"lat": lat, "lon": lon, "type": "adsb_icao", "seen_pos": 0.0, "seen": 0.0, "emergency": "none"} | kw


AIRCRAFT = [
    _at(90, 2, hex="a00001", flight="UAL123", r="N101UA", t="B738", alt_baro=5000, gs=250.0, track=75.0,
        baro_rate=2200, squawk="3301"),
    _at(0, 8, hex="a00002", flight="DAL45", r="N202DL", t="A321", alt_baro=35000, gs=460.0, track=180.0,
        baro_rate=0, squawk="4402"),
    _at(225, 12, hex="a00003", flight="SWA9", r="N303SW", t="B38M", alt_baro=9000, gs=280.0, track=300.0,
        baro_rate=-500, squawk="7700"),
    _at(160, 10, hex="a00004", flight="N12345", r="N12345", t="C172", alt_baro="ground", gs=8.0, track=10.0),
]


class FakeCollector:
    """Stands in for api.get: answers every collector endpoint the display calls, and logs aircraft requests."""

    def __init__(self):
        self.aircraft_calls: list[int] = []
        self.fail_aircraft = False

    def __call__(self, base, path, timeout=15):
        u = urlparse(path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/all":
            return {"now": "2026-11-17T17:00:00+00:00", "home": {"lat": HOME[0], "lon": HOME[1],
                                                                 "tz": "America/Los_Angeles"},
                    "labels": {"name": "TESTVILLE"}, "sources": {}, "radar_meta": None, "astro": {}}
        if u.path == "/api/geo":
            return {"home": list(HOME), "span_lon": 2.6, "towns": [["OAKLAND", 37.8044, -122.2712]],
                    "coast": [[[-122.60, 37.60], [-122.50, 37.80], [-122.45, 37.90]]], "roads": [], "border": [],
                    "state": [], "lakes": [], "airports": [["SFO", "San Francisco Intl", 37.6198, -122.3748, "L"]],
                    "runways": [[[-122.393, 37.612], [-122.358, 37.628]]]}
        if u.path == "/api/refresh":
            return {"ok": True}
        if u.path == "/api/radar":
            w, h = int(q["w"]), int(q["h"])
            rows = ["0" * w] * (h // 2) + ["3" * w] * (h - h // 2)   # rain over the south half
            n = int(q.get("last") or 3)
            return {"source": "test", "bbox": [0, 1, 0, 1], "w": w, "h": h,
                    "frames": [{"t": 1_800_000_000 + 600 * i, "rows": rows} for i in range(n)]}
        if u.path == "/api/aircraft":
            self.aircraft_calls.append(int(q["dist"]))
            if self.fail_aircraft:
                body = io.BytesIO(json.dumps({"error": "ADSB.FI 429; ADSB.LOL TimeoutError"}).encode())
                raise urllib.error.HTTPError(base + path, 502, "Bad Gateway", {}, body)
            return {"source": "ADSB.FI", "now": time.time(), "dist": int(q["dist"]), "home": list(HOME),
                    "ac": [dict(a) for a in AIRCRAFT]}
        if u.path == "/api/routes":
            known = {"UAL123": {"airline": "United Airlines", "iata": "UA123", "origin": SFO, "dest": ORD}}
            return {cs: known.get(cs) for cs in q["cs"].split(",")}
        raise AssertionError(f"unexpected collector call {path}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeCollector()
    monkeypatch.setattr(api, "get", fake)
    monkeypatch.setattr(wxapp, "STATE", tmp_path / "state.json")
    monkeypatch.setenv("WXTERM_API", "http://collector.test")
    monkeypatch.delenv("WXTERM_MAP", raising=False)
    fake.state = tmp_path / "state.json"
    return fake


def saved(fake) -> dict:
    return json.loads(fake.state.read_text())


async def wait_for(pilot, cond, timeout=6.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for the app")
        await pilot.pause(0.1)


def run(scenario, size=(240, 62)):
    """Run `scenario(app, pilot)` against a fresh WxApp in a headless terminal."""
    async def main():
        app = wxapp.WxApp()
        async with app.run_test(size=size) as pilot:
            await wait_for(pilot, lambda: app.data is not None and app.geo is not None)
            await scenario(app, pilot)
    asyncio.run(main())


async def to_air(app, pilot):
    await pilot.press("a")
    await wait_for(pilot, lambda: len(app.traffic.planes) == len(AIRCRAFT))


def text(app, sel="#air") -> str:
    return str(app.query_one(sel).render())


# --- which map ---------------------------------------------------------------------------------------------------

def test_starts_on_the_radar_map_and_fetches_no_aircraft(env):
    async def scenario(app, pilot):
        await pilot.pause(3)          # longer than one aircraft poll
        assert app.map_mode == "radar"
        assert app.query_one("#radar").display and not app.query_one("#air").display
        assert env.aircraft_calls == []
    run(scenario)


def test_a_switches_to_air_traffic_and_back(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        assert app.query_one("#air").display and not app.query_one("#radar").display
        assert env.aircraft_calls and env.aircraft_calls[0] > 25   # the radius reaches the map's corners
        assert saved(env)["map"] == "air"
        await pilot.press("a")
        await wait_for(pilot, lambda: app.radar and app.radar["w"] == app.query_one("#radar").dot_size()[0])
        assert app.map_mode == "radar" and app.query_one("#radar").display
        assert saved(env)["map"] == "radar"
    run(scenario)


def test_the_saved_map_comes_back_after_a_restart(env):
    env.state.write_text(json.dumps({"map": "air", "air_range": 60, "air_tags": "ALL", "air_wx": False}))

    async def scenario(app, pilot):
        await wait_for(pilot, lambda: len(app.traffic.planes) == len(AIRCRAFT))
        assert (app.map_mode, app.air_range, app.air_tags, app.air_wx) == ("air", 60, "ALL", False)
    run(scenario)


def test_wxterm_map_is_only_the_first_run_default(env, monkeypatch):
    monkeypatch.setenv("WXTERM_MAP", "air")
    assert wxapp.WxApp().map_mode == "air"
    env.state.write_text(json.dumps({"map": "radar"}))
    assert wxapp.WxApp().map_mode == "radar"      # the saved choice wins


def test_wxctl_map_command_switches_the_running_display(env):
    async def scenario(app, pilot):
        st = saved(env) | {"map": "air"}           # what scripts/wxctl.sh map air writes
        env.state.write_text(json.dumps(st))
        later = time.time() + 5
        os.utime(env.state, (later, later))
        app._watch_state()
        await wait_for(pilot, lambda: app.map_mode == "air" and app.traffic.planes)
    run(scenario)


# --- keys --------------------------------------------------------------------------------------------------------

def test_plus_and_minus_step_through_the_ranges(env):
    async def scenario(app, pilot):
        await pilot.press("plus")                  # radar map: ignored
        assert app.air_range == 25
        await to_air(app, pilot)
        await pilot.press("minus")
        assert app.air_range == 40
        await pilot.press("plus", "plus")
        assert app.air_range == 15
        await pilot.press(*["plus"] * 5)
        assert app.air_range == F.RANGES[0]
        await pilot.press(*["minus"] * 9)
        assert app.air_range == F.RANGES[-1]
        assert saved(env)["air_range"] == F.RANGES[-1]
    run(scenario)


def test_tab_selects_nearest_first_and_escape_clears(env):
    async def scenario(app, pilot):
        await pilot.press("tab")                   # radar map: Tab does nothing
        assert app.traffic.selected is None
        await to_air(app, pilot)
        await pilot.pause(0.6)                     # one frame, so the panel knows the order
        await pilot.press("tab")
        assert app.traffic.selected == "a00001"    # UAL123, 2 NM
        await pilot.press("tab")
        assert app.traffic.selected == "a00002"    # DAL45, 8 NM
        await pilot.press("shift+tab")
        assert app.traffic.selected == "a00001"
        await pilot.press("escape")
        assert app.traffic.selected is None
    run(scenario)


def test_air_keys_leave_tab_and_escape_alone_off_the_air_map_and_in_dialogs(env):
    from textual.screen import ModalScreen

    async def scenario(app, pilot):
        # on the radar map the air keys don't claim their keys, so Tab and Escape keep Textual's own jobs
        assert app.check_action("air_next", (1,)) is False and app.check_action("air_zoom", (-1,)) is False
        assert app.check_action("toggle_air", ()) is True
        await to_air(app, pilot)
        assert app.check_action("air_next", (1,)) is True
        assert app.check_action("air_select_none", ()) is False      # nothing to clear: Escape passes through
        app.air_select("a00001")
        assert app.check_action("air_select_none", ()) is True
        app.push_screen(ModalScreen())                                 # e.g. the command palette
        await pilot.pause(0.1)
        assert app.check_action("air_next", (1,)) is False and app.check_action("toggle_air", ()) is False
        await pilot.press("a")
        assert app.map_mode == "air"
    run(scenario)


def test_t_cycles_tags_and_w_toggles_the_radar_underneath(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        seen = []
        for _ in range(3):
            await pilot.press("t")
            seen.append(app.air_tags)
        assert seen == ["ALL", "OFF", "AUTO"]
        await pilot.press("w")
        assert app.air_wx is False and saved(env)["air_wx"] is False
        await pilot.press("w")
        assert app.air_wx is True
    run(scenario)


def test_clicking_a_traffic_list_row_selects_and_clicking_again_clears(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        await pilot.pause(0.6)
        panel = app.query_one("#air")
        def row():
            return next(h for h in panel.hot if h[4] == "a00002" and h[0] > panel.map_rect[2])

        r = row()
        await pilot.click("#air", offset=(r[0] + 2, r[1]))
        assert app.traffic.selected == "a00002"
        await pilot.pause(0.6)                     # the SELECTED box opens above the list and moves the rows down
        r2 = row()
        assert r2[1] > r[1]
        await pilot.click("#air", offset=(r2[0] + 2, r2[1]))
        assert app.traffic.selected is None
    run(scenario)


# --- what reaches the screen ---------------------------------------------------------------------------------------

def test_the_air_map_shows_traffic_routes_and_airports(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        await wait_for(pilot, lambda: isinstance(app.traffic.routes.get("UAL123"), dict))
        await wait_for(pilot, lambda: app.air_wxgrid is not None)
        await pilot.pause(0.6)
        s = text(app)
        assert "AIR TRAFFIC · ADSB.FI" in s and "ERROR" not in s
        assert "3 AIRBORNE · 1 GROUND" in s
        for cs in ("UAL123", "DAL45", "SWA9", "N12345"):
            assert cs in s
        assert "SFO›ORD" in s                      # the route fits, so it shows in the list
        assert "SFO" in s.replace("SFO›ORD", "")   # the airport label
        assert "● ADSB.FI" in text(app, "#status")
    run(scenario)


def test_selected_aircraft_details(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        await wait_for(pilot, lambda: isinstance(app.traffic.routes.get("UAL123"), dict))
        app.air_select("a00001")
        await pilot.pause(0.6)
        s = text(app)
        assert "SELECTED" in s and "UNITED AIRLINES" in s and "SAN FRANCISCO" in s and "CHICAGO" in s
        assert "ALT 5,000 FT" in s and "2.0 NM @ 090°" in s
    run(scenario)


def test_a_narrow_panel_drops_the_traffic_list(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        await pilot.pause(0.6)
        s = text(app)
        assert "AIR TRAFFIC" in s and "ERROR" not in s and "FLIGHT   TYPE" not in s
    run(scenario, size=(160, 45))


def test_a_feed_error_is_reported_and_the_last_picture_kept(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        env.fail_aircraft = True
        await wait_for(pilot, lambda: app.traffic.error, timeout=8)
        assert "ADSB.FI 429" in app.traffic.error
        assert len(app.traffic.planes) == len(AIRCRAFT)
        await pilot.pause(0.6)
        assert "FEED: ADSB.FI 429" in text(app)
    run(scenario)


def test_status_file_reports_the_air_map(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        app._write_status()
        st = json.loads((env.state.parent / "display-status.json").read_text())
        assert st["map_view"] == "air" and st["layout"]["map"] in ("left", "right")
        assert st["air"]["aircraft"] == len(AIRCRAFT) and st["air"]["feed"] == "ADSB.FI"
    run(scenario)
