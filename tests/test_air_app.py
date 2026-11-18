"""The map and its two layers (radar, air traffic) inside the running app, driven with Textual's test pilot against a
fake collector: which layers show at start, the keys, what gets saved, and what reaches the screen."""
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
        self.tle_calls: list[str] = []
        self.hazard_calls: list[tuple[str, str]] = []
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
        if u.path == "/api/tle":
            from test_space import ISS
            self.tle_calls.append(q["group"])
            return {"group": q["group"], "fetched": 1, "records": [ISS] if q["group"] == "stations" else []}
        if u.path == "/api/routes":
            known = {"UAL123": {"airline": "United Airlines", "iata": "UA123", "origin": SFO, "dest": ORD}}
            return {cs: known.get(cs) for cs in q["cs"].split(",")}
        if u.path == "/api/quakes":
            self.hazard_calls.append(("quakes", base))
            now = time.time()
            return {"fetched": now, "checked": now, "error": None, "sources": ["USGS"], "quakes": [
                {"id": "q1", "t": now - 300, "lat": HOME[0] + 0.15, "lon": HOME[1] + 0.2, "depth_km": 7.0,
                 "mag": 4.1, "mag_type": "ml", "place": "5 km E of Testville", "src": "USGS", "felt": 3,
                 "tsunami": False, "alert": None}]}
        if u.path == "/api/fires":
            self.hazard_calls.append(("fires", base))
            now = time.time()
            return {"fetched": now, "checked": now, "error": None, "sources": ["NIFC"], "perimeters": [], "fires": [
                {"id": "f1", "name": "MOUNTAIN", "lat": HOME[0] - 0.2, "lon": HOME[1] + 0.25, "acres": 1200,
                 "ha": None, "contained": 35, "discovered": now - 86400, "updated": now, "src": "NIFC",
                 "country": "US"}]}
        raise AssertionError(f"unexpected collector call {path}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeCollector()
    monkeypatch.setattr(api, "get", fake)
    monkeypatch.setattr(wxapp, "STATE", tmp_path / "state.json")
    monkeypatch.setenv("WXTERM_API", "http://collector.test")
    monkeypatch.delenv("WXTERM_AIR", raising=False)
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


async def radar_ready(app, pilot):
    panel = app.query_one(F.MapPanel)
    await wait_for(pilot, lambda: app.radar is not None and app.radar["w"] == panel.view()[0]
                   and app._radar_want[2] == app.air_range)


def text(app, sel="#mapview") -> str:
    return str(app.query_one(sel).render())


# --- layers ------------------------------------------------------------------------------------------------------

def test_starts_with_radar_on_air_traffic_off_and_fetches_no_aircraft(env):
    async def scenario(app, pilot):
        await radar_ready(app, pilot)
        await pilot.pause(3)          # longer than one aircraft poll
        assert (app.radar_on, app.air_on, app.air_range) == (True, False, F.DEFAULT_RANGE)
        assert env.aircraft_calls == []
        s = text(app)
        assert "RADAR · TEST" in s and "AIR TRAFFIC" not in s and "DBZ 10" in s and "ERROR" not in s
        assert "[A] AIR OFF  [W] RADAR ON  [O] SPACE ON  [E] QUAKES ON  [I] FIRES ON  [+/-] 40 NM" in s
        assert "FLIGHT   TYPE" not in s
    run(scenario)


def test_a_turns_air_traffic_on_over_the_radar_and_off_again(env):
    async def scenario(app, pilot):
        await radar_ready(app, pilot)
        narrow = app.query_one(F.MapPanel).view()[0]
        await to_air(app, pilot)
        assert app.air_on and app.radar_on and saved(env)["air"] is True
        assert env.aircraft_calls[0] > F.DEFAULT_RANGE      # the radius reaches the map's corners
        await radar_ready(app, pilot)                        # the traffic list took width: radar asked again
        assert app.radar["w"] < narrow
        await pilot.pause(0.6)
        s = text(app)
        assert "RADAR · TEST" in s and "AIR TRAFFIC · ADSB.FI" in s and "FLIGHT   TYPE" in s
        await pilot.press("a")
        assert not app.air_on and saved(env)["air"] is False
        await radar_ready(app, pilot)
        calls = len(env.aircraft_calls)
        await pilot.pause(3)
        assert len(env.aircraft_calls) == calls                # no polling while it's off
        assert "AIR TRAFFIC" not in text(app) and "FLIGHT   TYPE" not in text(app)
    run(scenario)


def test_w_turns_the_radar_off_and_on(env):
    async def scenario(app, pilot):
        await radar_ready(app, pilot)
        await pilot.press("w")
        assert app.radar_on is False and app.radar is None and saved(env)["radar"] is False
        await pilot.pause(0.4)
        s = text(app)
        assert "─ SPACE " in s.split("\n")[0] and "RADAR" not in s.split("\n")[0]   # only the space layer is left
        assert "RADAR ·" not in s and "DBZ" not in s and "[W] RADAR OFF" in s
        await pilot.press("w")
        assert app.radar_on and saved(env)["radar"] is True
        await radar_ready(app, pilot)
    run(scenario)


def test_all_layers_off_leaves_the_bare_map(env):
    async def scenario(app, pilot):
        await radar_ready(app, pilot)
        await pilot.press("w", "o", "e", "i")
        await pilot.pause(0.4)
        s = text(app)
        assert " MAP " in s.split("\n")[0] and "⌂ TESTVILLE" in s and "OAKLAND" in s and "SFO" in s and "ERROR" not in s
    run(scenario)


def test_the_saved_layers_come_back_after_a_restart(env):
    env.state.write_text(json.dumps({"air": True, "radar": False, "air_range": 60, "air_tags": "ALL"}))

    async def scenario(app, pilot):
        await wait_for(pilot, lambda: len(app.traffic.planes) == len(AIRCRAFT))
        assert (app.air_on, app.radar_on, app.air_range, app.air_tags) == (True, False, 60, "ALL")
        assert app.radar is None
    run(scenario)


def test_wxterm_air_is_only_the_first_run_default(env, monkeypatch):
    assert wxapp.WxApp().air_on is False
    monkeypatch.setenv("WXTERM_AIR", "on")
    assert wxapp.WxApp().air_on is True
    env.state.write_text(json.dumps({"air": False}))
    assert wxapp.WxApp().air_on is False      # the saved choice wins


def test_wxctl_air_and_radar_commands_switch_the_running_display(env):
    async def scenario(app, pilot):
        st = saved(env) | {"air": True, "radar": False}   # what wxctl.sh air on / radar off write
        env.state.write_text(json.dumps(st))
        later = time.time() + 5
        os.utime(env.state, (later, later))
        app._watch_state()
        await wait_for(pilot, lambda: app.air_on and app.traffic.planes and not app.radar_on)
        st = saved(env) | {"air_range": 15, "air_tags": "ALL"}   # wxctl.sh range 15 / tags all
        env.state.write_text(json.dumps(st))
        later += 5
        os.utime(env.state, (later, later))
        app._watch_state()
        assert (app.air_range, app.air_tags) == (15, "ALL")
        assert (saved(env)["air_range"], saved(env)["air_tags"]) == (15, "ALL")   # the file isn't written back
    run(scenario)


# --- keys --------------------------------------------------------------------------------------------------------

def test_plus_and_minus_step_through_the_ranges_with_or_without_traffic(env):
    async def scenario(app, pilot):
        await radar_ready(app, pilot)
        await pilot.press("minus")                 # radar only: the range still changes, and the radar follows
        assert app.air_range == 60 and saved(env)["air_range"] == 60
        await radar_ready(app, pilot)
        await to_air(app, pilot)
        await pilot.press("plus", "plus", "plus")
        assert app.air_range == 15
        await pilot.press(*["plus"] * 5)
        assert app.air_range == F.RANGES[0]
        await pilot.press(*["minus"] * 9)
        assert app.air_range == F.RANGES[-1]
        assert saved(env)["air_range"] == F.RANGES[-1]
        await radar_ready(app, pilot)
    run(scenario)


def test_tab_selects_nearest_first_and_escape_clears(env):
    async def scenario(app, pilot):
        await pilot.press("tab")                   # air traffic off: Tab does nothing
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


def test_map_keys_leave_tab_and_escape_alone_without_traffic_and_in_dialogs(env):
    from textual.screen import ModalScreen

    async def scenario(app, pilot):
        # without air traffic its keys don't claim Tab and Escape, so they keep Textual's own jobs
        assert app.check_action("air_next", (1,)) is False and app.check_action("air_tags", ()) is False
        assert app.check_action("toggle_air", ()) is True and app.check_action("zoom", (-1,)) is True
        await to_air(app, pilot)
        assert app.check_action("air_next", (1,)) is True
        assert app.check_action("air_select_none", ()) is False      # nothing to clear: Escape passes through
        app.air_select("a00001")
        assert app.check_action("air_select_none", ()) is True
        app.push_screen(ModalScreen())                                 # e.g. the command palette
        await pilot.pause(0.1)
        for action, params in (("air_next", (1,)), ("toggle_air", ()), ("toggle_radar", ()), ("zoom", (1,))):
            assert app.check_action(action, params) is False
        await pilot.press("a", "w")
        assert app.air_on and app.radar_on
    run(scenario)


def test_t_cycles_the_data_tags(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        seen = []
        for _ in range(3):
            await pilot.press("t")
            seen.append(app.air_tags)
        assert seen == ["ALL", "OFF", "AUTO"]
    run(scenario)


def test_clicking_a_traffic_list_row_selects_and_clicking_again_clears(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        await pilot.pause(0.6)
        panel = app.query_one("#mapview")
        def row():
            return next(h for h in panel.hot if h[4] == "a00002" and h[0] > panel.map_rect[2])

        r = row()
        await pilot.click("#mapview", offset=(r[0] + 2, r[1]))
        assert app.traffic.selected == "a00002"
        await pilot.pause(0.6)                     # the SELECTED box opens above the list and moves the rows down
        r2 = row()
        assert r2[1] > r[1]
        await pilot.click("#mapview", offset=(r2[0] + 2, r2[1]))
        assert app.traffic.selected is None
    run(scenario)


# --- what reaches the screen ---------------------------------------------------------------------------------------

def test_the_air_map_shows_traffic_routes_and_airports(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        await wait_for(pilot, lambda: isinstance(app.traffic.routes.get("UAL123"), dict))
        await radar_ready(app, pilot)
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


def test_status_file_reports_the_layers(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        app._write_status()
        st = json.loads((env.state.parent / "display-status.json").read_text())
        assert st["layers"] == {"radar": True, "air": True, "space": True, "quakes": True, "fires": True}
        assert st["range_nm"] == F.DEFAULT_RANGE
        assert st["layout"]["map"] in ("left", "right")
        assert st["air"]["aircraft"] == len(AIRCRAFT) and st["air"]["feed"] == "ADSB.FI"
        await pilot.press("a")
        app._write_status()
        st = json.loads((env.state.parent / "display-status.json").read_text())
        assert st["layers"] == {"radar": True, "air": False, "space": True, "quakes": True,
                                "fires": True} and st["air"] is None
    run(scenario)


# --- suspend and resume ------------------------------------------------------------------------------------------

def test_waking_from_suspend_drops_old_aircraft_and_radar_and_refreshes(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        await radar_ready(app, pilot)
        env.aircraft_calls.clear()
        app._last_tick -= 3600                     # as if the 1 s tick last ran an hour ago: the machine slept
        app._tick()
        assert app.waking() and not app.traffic.planes and app.radar is None
        assert "AWAKE AFTER 1H 00M" in app.gif_msg
        await wait_for(pilot, lambda: len(app.traffic.planes) == len(AIRCRAFT) and app.radar is not None)
        assert env.aircraft_calls                  # fetched again, not drawn from before the sleep
    run(scenario)


def test_network_errors_right_after_waking_are_not_shown(env):
    async def scenario(app, pilot):
        await to_air(app, pilot)
        app._last_tick -= 600
        app._tick()
        env.fail_aircraft = True
        app._set_data(app.base, None, "HOME URLError: network is unreachable")
        await pilot.pause(3)
        assert app.api_error is None and not app.traffic.error
        app.resumed_at -= app.WAKE_GRACE + 1       # later on, errors are real again
        app._set_data(app.base, None, "HOME URLError: network is unreachable")
        assert app.api_error
    run(scenario)


# --- spacecraft ----------------------------------------------------------------------------------------------------

def _overhead(app):
    """Pretend the ISS is right over home now, with a short track."""
    from wxterm import space as S
    lat, lon = HOME
    craft = S.Craft(25544, "ISS", True)
    app.sky.cand = [0]
    app.sky.now = lambda idx, t, bbox: [(craft, lat, lon, 418.0, [(lat - 0.2, lon - 0.3), (lat, lon),
                                                                   (lat + 0.2, lon + 0.3)])]


def test_space_is_on_by_default_and_fetches_elements_from_the_first_collector(env):
    async def scenario(app, pilot):
        assert app.space_on
        await wait_for(pilot, lambda: len(app.sky) == 1)
        assert set(env.tle_calls) == {"stations", "visual", "starlink"}
        await wait_for(pilot, lambda: app.space_passes is not None)
        assert "[O] SPACE ON" in text(app) and " SPACE " in text(app).split("\n")[0]
    run(scenario)


def test_a_station_over_the_map_is_drawn_and_named_in_the_border(env):
    async def scenario(app, pilot):
        await wait_for(pilot, lambda: len(app.sky) == 1)
        _overhead(app)
        app._refresh_map()
        await pilot.pause(0.6)
        s = text(app)
        # the town is just right of the station, so the label goes left and both stay readable
        assert "ISS 418 KM" in s and "◆ TESTVILLE" in s and "ISS OVER THE MAP" in s.split("\n")[0]
    run(scenario)


def test_o_turns_the_space_layer_off_and_saves_it(env):
    async def scenario(app, pilot):
        await wait_for(pilot, lambda: len(app.sky) == 1)
        _overhead(app)
        await pilot.press("o")
        assert app.space_on is False and saved(env)["space"] is False
        await pilot.pause(0.6)
        s = text(app)
        assert "◆" not in s and "[O] SPACE OFF" in s
    run(scenario)


def test_the_passes_panel(env):
    from wxterm import space as S

    async def scenario(app, pilot):
        await wait_for(pilot, lambda: app.space_passes is not None)
        t0 = time.time() + 3600
        app.space_passes = [S.Pass("ISS", t0, t0 + 300, t0 + 600, 62.0, "SW", "NE", True),
                            S.Pass("TIANGONG", t0 + 9000, t0 + 9200, t0 + 9400, 18.0, "W", "S", False)]
        app.space_next = ("ISS", time.time() + 2 * 86400)
        slots = [k for k, _ in app.query_one("#extras").available(app)]
        app.extras_idx = slots.index("passes")
        app.query_one("#extras").refresh()
        await pilot.pause(0.4)
        s = text(app, "#extras")
        assert "VISIBLE FROM TESTVILLE" in s and "ISS" in s and "62°" in s and "SW → NE" in s and "10 MIN" in s
        assert "TIANGONG" not in s.split("NEXT OVER")[0]           # not visible: not listed
        assert "NEXT OVER THE MAP: ISS" in s
    run(scenario)


# --- earthquakes and wildfires -------------------------------------------------------------------------------------

def test_quakes_and_fires_are_on_by_default_and_drawn_on_the_map(env):
    async def scenario(app, pilot):
        assert app.quake_on and app.fire_on
        await wait_for(pilot, lambda: app.quakes is not None and app.fires is not None)
        assert {w for w, _ in env.hazard_calls} == {"quakes", "fires"}
        await wait_for(pilot, lambda: "M4.1 5M AGO" in text(app))
        s = text(app)
        top = s.split("\n")[0]
        assert " QUAKES " in top and " FIRES" in top and "M4.1 QUAKE 5M AGO" in top and "1 FIRE" in top
        assert "▲" in s and "MOUNTAIN 1,200 AC 35%" in s and "ERROR" not in s
    run(scenario)


def test_e_and_i_switch_the_layers_and_are_saved(env):
    async def scenario(app, pilot):
        await wait_for(pilot, lambda: "M4.1" in text(app))
        await pilot.press("e")
        await pilot.pause(0.3)
        s = text(app)
        assert not app.quake_on and "M4.1" not in s and "[E] QUAKES OFF" in s and "MOUNTAIN" in s
        await pilot.press("i")
        await pilot.pause(0.3)
        s = text(app)
        assert not app.fire_on and "MOUNTAIN" not in s and "[I] FIRES OFF" in s
        assert saved(env)["quakes"] is False and saved(env)["fires"] is False
        n = len(env.hazard_calls)
        app._fetch_quakes_bg()
        app._fetch_fires_bg()
        await pilot.pause(0.3)
        assert len(env.hazard_calls) == n   # nothing is fetched while the layers are off
    run(scenario)


def test_hazard_layers_can_start_off(env, monkeypatch):
    monkeypatch.setenv("WXTERM_QUAKES", "off")
    monkeypatch.setenv("WXTERM_FIRES", "off")

    async def scenario(app, pilot):
        await pilot.pause(0.5)
        assert not app.quake_on and not app.fire_on and env.hazard_calls == []
        assert "[E] QUAKES OFF  [I] FIRES OFF" in text(app)
    run(scenario)
