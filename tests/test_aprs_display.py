"""The display's APRS side: symbols, the overlay on the radar map, the APRS map, the extras slots and the keys."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from wxterm import aprs
from wxterm.canvas import Canvas
from wxterm.themes import THEMES

ROOT = Path(__file__).resolve().parent.parent
HOME = [37.7749, -122.4194]


def stn(call, lat, lon, sym="/-", **kw):
    return {"call": call, "lat": lat, "lon": lon, "sym": sym, "t": time.time() - 60, "n": 1, "track": [], **kw}


STATIONS = [
    stn("FW1234", 37.80, -122.45, "/_", wx={"temp_f": 45.0, "humidity": 65, "wind_dir": 180, "wind_mph": 5.0,
                                            "pressure_mb": 1013.2, "rain_today_in": 0.1}),
    stn("KC1AWV-9", 37.70, -122.30, "/>", course=90, speed_mph=40,
        track=[[37.70, -122.40, int(time.time()) - 600], [37.70, -122.30, int(time.time()) - 60]]),
    stn("W6DIGI", 37.90, -122.20, "/#", comment="digi on the hill"),
    stn("K6HOME", 37.60, -122.50, "/-", wx={"temp_f": 51.0}),
]


# --- symbols ---------------------------------------------------------------------------------------------------

def test_kinds_and_glyphs():
    wx, car, digi, house_wx = STATIONS
    assert (aprs.kind(wx), aprs.glyph(wx)) == ("WX", "+")
    assert (aprs.kind(car), aprs.glyph(car)) == ("MOBILE", "→")
    assert aprs.glyph(dict(car, speed_mph=0)) == "○"   # parked
    assert (aprs.kind(digi), aprs.glyph(digi)) == ("DIGI", "◆")
    assert aprs.kind(house_wx) == "WX"                 # a house symbol sending weather counts as a weather station
    assert aprs.kind(stn("X", 0, 0, "/Q")) == "STN" and aprs.kind(stn("X", 0, 0, "/Q", obj=True)) == "OBJ"


# --- the overlay --------------------------------------------------------------------------------------------------

def _proj(c):
    lon0, lon1, lat0, lat1 = -122.7, -122.0, 37.4, 38.1
    return lambda lon, lat: ((lon - lon0) / (lon1 - lon0) * (c.w * 2 - 1), (lat1 - lat) / (lat1 - lat0) * (c.h * 4 - 1))


def _text(c):
    return "\n".join("".join(ch if c.text[y][x] else " " for x, ch in enumerate(row)) for y, row in enumerate(c.chars))


def test_wx_overlay_draws_only_temperatures():
    c = Canvas(80, 24)
    aprs.draw_stations(c, THEMES[0], STATIONS, _proj(c), "WX", time.time())
    txt = _text(c)
    assert "45°" in txt and "51°" in txt and "KC1AWV" not in txt and "◆" not in txt


def test_all_overlay_labels_never_cover_other_text():
    c = Canvas(80, 24)
    c.put(0, 12, "·" + "TOWN" * 19, "")  # a row of town labels
    before = _text(c).splitlines()[12]
    rows = STATIONS + [stn("N0ROW", 37.75 - 0.0001 * i, -122.65 + 0.03 * i, "/-") for i in range(15)]
    aprs.draw_stations(c, THEMES[0], rows, _proj(c), "ALL", time.time())
    txt = _text(c)
    assert _text(c).splitlines()[12] == before
    assert "KC1AWV-9" in txt and "W6DIGI" in txt and "45°" in txt and "→" in txt
    assert any(c.dots[y][x] for y in range(c.h) for x in range(c.w))  # the car's track


# --- the app, end to end against a fake collector ---------------------------------------------------------------

class FakeCollector(BaseHTTPRequestHandler):
    APRS = True   # False: a location without APRS (no "aprs" source, /api/aprs disabled)

    def log_message(self, *a):
        pass

    def do_GET(self):
        from wxcollect.radar import bbox_for
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        if u.path == "/api/all":
            ok = {"ok_at": now, "error": None, "interval": 30}
            body = {"now": now, "home": {"lat": HOME[0], "lon": HOME[1], "tz": "America/Los_Angeles"},
                    "labels": {"name": "SAN FRANCISCO"}, "astro": {}, "radar_meta": {"ok_at": now},
                    "sources": {"aprs": ok | {"data": {"connected": True, "server": "T2TEST", "stations": 4}},
                                "obs": ok | {"data": {"station": "KSFO", "temp_f": 48.0, "humidity": 70}}}}
            if not self.APRS:
                del body["sources"]["aprs"]
        elif u.path == "/api/geo":
            body = json.loads((ROOT / "locations" / "sf.geo.json").read_text()) | {
                "towns": [["OAKLAND", 37.8044, -122.2712]], "home": HOME, "span_lon": 2.6}
        elif u.path == "/api/radar":
            w, h = int(q["w"]), int(q["h"])
            bbox = [float(v) for v in q["bbox"].split(",")] if "bbox" in q else bbox_for(w, h, lat=HOME[0], lon=HOME[1])
            rows = ["0" * w] * h
            body = {"source": "test", "bbox": bbox, "w": w, "h": h, "frames": [{"t": time.time(), "rows": rows}]}
        elif u.path == "/api/aprs":
            body = {"enabled": True, "now": time.time(), "home": HOME, "call": "KC1AWV", "range_km": 150,
                    "status": {"connected": True, "server": "T2TEST", "packets": 42}, "stations": STATIONS} \
                if self.APRS else {"enabled": False, "stations": []}
        else:
            body = {"ok": True}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def collector(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCollector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("WXTERM_API", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("WXTERM_APRS_VIEW", "ALL")
    yield
    srv.shutdown()


async def _ready(app, pilot):
    for _ in range(80):
        await pilot.pause(0.1)
        if app.data and app.radar and app.geo and app.aprs:
            return
    raise AssertionError("no data from the fake collector")


def test_app_views_keys_and_slots(collector, tmp_path, monkeypatch):
    import asyncio
    from wxterm import app as appmod
    monkeypatch.setattr(appmod, "STATE", tmp_path / "state.json")

    async def go():
        app = appmod.WxApp()
        async with app.run_test(size=(240, 62)) as pilot:
            await _ready(app, pilot)
            await pilot.pause(0.3)
            radar = str(app.query_one("#radar").render())
            assert "KC1AWV-9" in radar and "45°" in radar and "ERROR" not in radar
            assert "APRS ALL [X]" in str(app.query_one("#status").render())

            await pilot.press("x")   # MAP: the APRS map replaces the radar loop
            await pilot.pause(0.5)
            assert app.aprs_view == "MAP" and not app.query_one("#radar").display
            m = str(app.query_one("#aprsmap").render())
            assert "APRS · T2TEST · 4 STATIONS" in m and "RANGE 25 MI" in m and "ERROR" not in m
            assert "HEARD" in m and "W6DIGI" in m and "LOGIN KC1AWV RX ONLY" in m
            assert app.aprs_wxgrid and app.aprs_wxgrid["frames"]
            await pilot.press("right_square_bracket")
            await pilot.pause(0.2)
            assert app.aprs_range == 50 and "RANGE 50 MI" in str(app.query_one("#aprsmap").render())

            await pilot.press("x")   # OFF: back to the plain radar loop; [ ] do nothing there
            await pilot.pause(0.3)
            assert app.aprs_view == "OFF" and app.query_one("#radar").display
            assert "KC1AWV-9" not in str(app.query_one("#radar").render())
            await pilot.press("left_square_bracket")
            assert app.aprs_range == 50

            await pilot.press("x")   # WX: weather stations' temperatures only
            await pilot.pause(0.3)
            radar = str(app.query_one("#radar").render())
            assert app.aprs_view == "WX" and "45°" in radar and "51°" in radar and "KC1AWV-9" not in radar

            slots = [k for k, _ in app.query_one("#extras").available(app)]
            assert "aprs" in slots and "aprswx" in slots
            for k in ("aprs", "aprswx"):
                app.extras_idx = slots.index(k)
                app.query_one("#extras").refresh()
                await pilot.pause(0.2)
                txt = str(app.query_one("#extras").render())
                assert "ERROR" not in txt and "FW1234" in txt
            assert "KSFO" in txt and "45°" in txt   # the official observation heads the weather table

            st = json.loads((tmp_path / "state.json").read_text())
            assert st["aprs_view"] == "WX" and st["aprs_range"] == 50

    asyncio.run(go())


def test_location_without_aprs(collector, tmp_path, monkeypatch):
    import asyncio
    from wxterm import app as appmod
    monkeypatch.setattr(appmod, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(FakeCollector, "APRS", False)

    async def go():
        app = appmod.WxApp()
        async with app.run_test(size=(240, 62)) as pilot:
            for _ in range(80):
                await pilot.pause(0.1)
                if app.data and app.radar and app.geo:
                    break
            await pilot.pause(0.3)
            assert app.aprs is None and not aprs.enabled(app)
            assert [k for k, _ in app.query_one("#extras").available(app)] == ["afd", "surf", "transect"]
            assert "APRS-IS" not in str(app.query_one("#status").render())
            app._set_aprs_view("MAP", save=False)
            await pilot.pause(0.3)
            m = str(app.query_one("#aprsmap").render())
            assert "COLLECTOR ISN'T RUNNING APRS" in m and "GEN-COMPOSE" in m and "ERROR" not in m

    asyncio.run(go())
