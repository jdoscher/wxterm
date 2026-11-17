"""The collector's HTTP endpoints for the AIR map (collector/wxcollect/main.py), served on a free local port, with the
lookups behind them replaced. Also /api/radar's `last` option."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from wxcollect import main as collector
from wxcollect.radar import EquiFrame, Radar


def frames(n=3):
    """n tiny IEM-style frames (already decoded to levels) over a 1x1 degree box, all level 2 rain."""
    return {f"f{i}": EquiFrame(1_800_000_000 + 600 * i, -123.0, 38.0, 0.25, 4, 4, bytes([2] * 16))
            for i in range(n)}


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), collector.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_aircraft_endpoint(server, monkeypatch):
    asked = []
    monkeypatch.setattr(collector.flights, "aircraft",
                        lambda d: asked.append(d) or {"source": "ADSB.FI", "dist": d, "ac": []})
    assert get(f"{server}/api/aircraft?dist=30") == (200, {"source": "ADSB.FI", "dist": 30, "ac": []})
    get(f"{server}/api/aircraft?dist=12.9")
    get(f"{server}/api/aircraft")
    assert asked == [30, 12, 40]          # fractional radii truncate; 40 NM without one


def test_aircraft_endpoint_reports_feed_failure_as_502(server, monkeypatch):
    def down(_d):
        raise RuntimeError("ADSB.FI 429; ADSB.LOL TimeoutError")
    monkeypatch.setattr(collector.flights, "aircraft", down)
    assert get(f"{server}/api/aircraft?dist=30") == (502, {"error": "ADSB.FI 429; ADSB.LOL TimeoutError"})


def test_routes_endpoint(server, monkeypatch):
    monkeypatch.setattr(collector.flights, "routes", lambda cs: {c: None for c in cs})
    assert get(f"{server}/api/routes?cs=UAL1,DAL2") == (200, {"UAL1": None, "DAL2": None})


def test_geo_endpoint_serves_airports_and_runways(server):
    code, g = get(f"{server}/api/geo")
    assert code == 200
    assert any(a[0] == "SFO" and a[4] == "L" for a in g["airports"])
    assert g["runways"] and all(len(r) == 2 and len(r[0]) == 2 for r in g["runways"])


def test_radar_endpoint_can_return_only_the_newest_frames(server, monkeypatch):
    monkeypatch.setattr(collector.RADAR, "frames", frames(3))
    code, every = get(f"{server}/api/radar?w=4&h=4&bbox=-123,-122,37,38")
    assert code == 200 and [f["t"] for f in every["frames"]] == [1_800_000_000, 1_800_000_600, 1_800_001_200]
    _, newest = get(f"{server}/api/radar?w=4&h=4&bbox=-123,-122,37,38&last=1")
    assert [f["t"] for f in newest["frames"]] == [1_800_001_200]
    assert newest["frames"][0]["rows"] == every["frames"][-1]["rows"] == ["2222"] * 4


@pytest.mark.parametrize("last, n", [(0, 3), (1, 1), (2, 2), (9, 3)])
def test_radar_grid_last(last, n):
    r = Radar()
    r.frames = frames(3)
    out = r.grid(4, 4, (-123, -122, 37, 38), last=last)
    assert len(out["frames"]) == n
    assert out["frames"][-1]["t"] == 1_800_001_200
