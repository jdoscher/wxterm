"""The collector's APRS side: packets into the station table, the APRS-IS session, and /api/aprs."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from wxcollect import aprs, aprsparse, config

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    aprs.STATIONS.clear()
    aprs.STATUS.update(connected=False, server="", since=None, alive_at=None, last_packet=None, packets=0,
                       verified=None, error=None)
    monkeypatch.setattr(config, "APRS_CALL", "KC1AWV")
    monkeypatch.setattr(config, "APRS_RANGE_KM", 150.0)
    yield
    aprs.STATIONS.clear()


def feed(line: str, now: float = NOW) -> str | None:
    return aprs.ingest(aprsparse.parse(line), now)


# SF is home in the tests (conftest); these positions are around the bay
CAR = "KC1AWV-9>APRS,WIDE1-1,qAR,K6IGT:!3746.50N/12225.16W>090/035/A=000123 mobile"
WX = "FW1234>APRS,TCPXX*,qAX,CWOP-2:@171415z3748.00N/12227.00W_180/005g012t045r000p012P010h65b10132L000.WD 31"


def test_mobile_position_and_track():
    assert feed(CAR) == "KC1AWV-9"
    s = aprs.STATIONS["KC1AWV-9"]
    assert s["sym"] == "/>" and s["comment"] == "mobile" and s["rf"] is True and s["via"] == "K6IGT"
    assert s["course"] == 90 and s["speed_mph"] == pytest.approx(40.3, abs=0.1) and s["alt_ft"] == 123
    feed(CAR, NOW + 30)  # same spot: no new track point
    assert len(s["track"]) == 1 and s["n"] == 2
    feed(CAR.replace("3746.50N", "3746.60N"), NOW + 60)  # ~185 m north
    assert len(s["track"]) == 2 and s["track"][-1][2] == int(NOW + 60)


def test_weather_station_units_and_wind_from_course():
    feed(WX)
    s = aprs.STATIONS["FW1234"]
    wx = s["wx"]
    assert wx["temp_f"] == 45.0 and wx["humidity"] == 65 and wx["pressure_mb"] == 1013.2
    assert wx["wind_dir"] == 180 and wx["wind_mph"] == pytest.approx(5.0, abs=0.1)
    assert wx["gust_mph"] == pytest.approx(12.0, abs=0.1) and wx["rain_24h_in"] == pytest.approx(0.12)
    assert "course" not in s and "speed_mph" not in s  # a weather station's course/speed are wind, not motion


def test_positionless_weather_merges_into_known_station():
    feed(WX)
    feed("FW1234>APRS,qAC,T2:_10090556c220s004g005t077r000p000P000h50b09900wRSW", NOW + 60)
    s = aprs.STATIONS["FW1234"]
    assert s["wx"]["temp_f"] == 77.0 and s["wx"]["wind_dir"] == 220 and s["lat"] == pytest.approx(37.8)
    assert s["comment"] == ".WD 31"   # the positionless packet's software tag doesn't replace the comment


def test_objects_and_killed_objects():
    feed("KC1AWV>APRS,TCPIP*,qAC,T2:;NET-CTRL *092345z3747.00N/12224.00Wo net control")
    o = aprs.STATIONS["NET-CTRL"]
    assert o["obj"] is True and o["owner"] == "KC1AWV" and o["sym"] == "/o"
    feed("KC1AWV>APRS,TCPIP*,qAC,T2:;NET-CTRL _092345z3747.00N/12224.00Wo")
    assert "NET-CTRL" not in aprs.STATIONS


def test_far_away_positions_messages_and_status():
    assert feed("N0FAR>APRS,TCPIP*,qAC,T2:!4237.14N/07120.83W-boston") is None  # Boston, not near SF
    assert feed("N0CALL>APRS,TCPIP*,qAC,T2::KC1AWV   :hello{1") is None
    assert feed("N0NEW>APRS,TCPIP*,qAC,T2:>status before any position") is None
    feed(CAR)
    feed("KC1AWV-9>APRS,TCPIP*,qAC,T2:>on the way home", NOW + 5)
    assert aprs.STATIONS["KC1AWV-9"]["status"] == "on the way home"


def test_prune_and_snapshot(monkeypatch):
    monkeypatch.setattr(config, "APRS_MAX_AGE_MIN", 60.0)
    feed(CAR, NOW - 3700)
    feed(WX, NOW)
    aprs.prune(NOW)
    assert list(aprs.STATIONS) == ["FW1234"]
    monkeypatch.setattr(aprs.time, "time", lambda: NOW)
    snap = aprs.snapshot()
    assert snap["enabled"] and snap["call"] == "KC1AWV" and [s["call"] for s in snap["stations"]] == ["FW1234"]


def test_summary_raises_until_connected(monkeypatch):
    monkeypatch.setattr(aprs, "save", lambda: None)
    with pytest.raises(RuntimeError, match="connecting"):
        aprs.summary()
    aprs.STATUS.update(connected=True, alive_at=time.time())
    feed(WX, time.time())
    out = aprs.summary()
    assert out["stations"] == 1 and out["wx_stations"] == 1 and out["call"] == "KC1AWV"


def test_login_line_is_receive_only():
    line = aprs.login_line().decode()
    assert line.startswith("user KC1AWV pass -1 vers wxterm") and line.endswith("\r\n")
    assert f"filter r/{config.LAT:.1f}/{config.LON:.1f}/150" in line   # home rounded to 0.1°


def test_run_refuses_without_a_callsign(monkeypatch):
    monkeypatch.setattr(config, "APRS_CALL", "")
    aprs.run()  # returns at once instead of connecting
    assert "APRS_CALL" in aprs.STATUS["error"]


def test_session_against_a_fake_aprs_is_server():
    """Banner, login, logresp, keepalive, packets, an unparseable line, then the server hangs up."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {}

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.sendall(b"# aprsc 2.1.19 test\r\n")
            got["login"] = conn.makefile("rb").readline()
            conn.sendall(b"# logresp KC1AWV unverified, server T2TEST\r\n# keepalive\r\n")
            conn.sendall((CAR + "\r\n" + WX + "\r\nthis is not aprs\r\n").encode())

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    with pytest.raises(ConnectionError, match="closed"):
        aprs.session("127.0.0.1", srv.getsockname()[1])
    th.join(5)
    srv.close()
    assert got["login"] == aprs.login_line()
    assert aprs.STATUS["server"] == "T2TEST" and aprs.STATUS["verified"] is False
    assert aprs.STATUS["packets"] == 2 and set(aprs.STATIONS) == {"KC1AWV-9", "FW1234"}
    assert aprs.STATUS["connected"] is False


def test_api_aprs_endpoint(monkeypatch):
    from wxcollect import main
    feed(WX, time.time())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/aprs"
    try:
        monkeypatch.setattr(config, "APRS", False)
        assert json.loads(urllib.request.urlopen(url, timeout=5).read()) == {"enabled": False, "stations": []}
        monkeypatch.setattr(config, "APRS", True)
        j = json.loads(urllib.request.urlopen(url, timeout=5).read())
        assert j["enabled"] and j["stations"][0]["wx"]["temp_f"] == 45.0
    finally:
        srv.shutdown()
