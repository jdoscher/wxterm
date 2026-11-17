"""The collector's aircraft and route lookups (collector/wxcollect/flights.py), with the network replaced by a fake:
which feed is used, fallback and backoff, caching, and how answers are trimmed."""
from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from wxcollect import flights


@pytest.fixture
def fl(monkeypatch):
    """flights with clean caches, no request spacing, and a scripted network."""
    monkeypatch.setattr(flights, "_ac_cache", {})
    monkeypatch.setattr(flights, "_routes", {})
    monkeypatch.setattr(flights, "_fi_backoff_until", 0.0)
    for gate in (flights._FI_GATE, flights._LOL_GATE, flights._ROUTE_GATE):
        monkeypatch.setattr(gate, "gap", 0.0)
    net = Net()
    monkeypatch.setattr(flights, "get", net)
    return net


class Net:
    """Fake net.get: answers by URL prefix with bytes, or raises; records every URL asked for."""

    def __init__(self):
        self.urls: list[str] = []
        self.answers: dict[str, object] = {}

    def __call__(self, url, timeout=25, tries=2, accept=None):
        self.urls.append(url)
        for prefix, ans in self.answers.items():
            if url.startswith(prefix):
                if isinstance(ans, Exception):
                    raise ans
                return json.dumps(ans).encode()
        raise AssertionError(f"unexpected request {url}")

    def count(self, prefix):
        return sum(u.startswith(prefix) for u in self.urls)


FI, LOL, DB = "https://opendata.adsb.fi/", "https://api.adsb.lol/", "https://api.adsbdb.com/"


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "nope", {}, io.BytesIO(b""))


ROW = {"hex": "a1", "lat": 37.7, "lon": -122.4, "flight": "UAL123  ", "alt_baro": 5000, "gs": 250.1,
       "category": "A3", "mlat": [], "tisb": ["lat", "lon"], "rssi": -20.5, "nic": 8, "track": None}


# --- trimming --------------------------------------------------------------------------------------------------------

def test_slim_keeps_flights_with_a_position_and_only_useful_fields():
    rows = [ROW, {"hex": "nopos", "flight": "X"}, {"hex": "veh", "lat": 1, "lon": 2, "category": "C1"},
            {"hex": "str", "lat": "37", "lon": "-122"}]
    out = flights._slim(rows)
    assert [o["hex"] for o in out] == ["a1"]
    a = out[0]
    assert a["flight"] == "UAL123"                     # padding stripped
    assert a["mlat"] is False and a["tisb"] is True    # field lists become flags
    assert "rssi" not in a and "nic" not in a          # not needed by the display
    assert "track" not in a                            # nulls dropped
    assert set(a) <= set(flights.KEEP)


def test_slim_handles_a_missing_list():
    assert flights._slim(None) == []


# --- aircraft --------------------------------------------------------------------------------------------------------

def test_aircraft_come_from_adsb_fi_around_home(fl):
    fl.answers[FI] = {"aircraft": [ROW], "now": 1}
    j = flights.aircraft(30)
    assert j["source"] == "ADSB.FI" and j["dist"] == 30 and len(j["ac"]) == 1
    assert j["home"] == [37.7749, -122.4194]
    assert fl.urls == ["https://opendata.adsb.fi/api/v2/lat/37.7749/lon/-122.4194/dist/30"]
    assert abs(j["now"] - time.time()) < 5


@pytest.mark.parametrize("asked, sent", [(999, 250), (0, 1), (-5, 1), (40.7, 40)])
def test_radius_is_clamped_to_what_the_feeds_serve(fl, asked, sent):
    fl.answers[FI] = {"aircraft": []}
    assert flights.aircraft(asked)["dist"] == sent
    assert fl.urls[-1].endswith(f"/dist/{sent}")


def test_answers_are_cached_for_a_moment(fl):
    fl.answers[FI] = {"aircraft": [ROW]}
    flights.aircraft(30)
    flights.aircraft(30)
    assert fl.count(FI) == 1
    t, ans = flights._ac_cache[30]
    flights._ac_cache[30] = (t - 2, ans)              # 2 s later
    flights.aircraft(30)
    assert fl.count(FI) == 2


def test_falls_back_to_adsb_lol_and_backs_off_adsb_fi(fl):
    fl.answers[FI] = http_error(429)
    fl.answers[LOL] = {"ac": [ROW], "now": 1}
    j = flights.aircraft(30)
    assert j["source"] == "ADSB.LOL" and len(j["ac"]) == 1
    assert fl.urls[-1] == "https://api.adsb.lol/v2/point/37.7749/-122.4194/30"
    assert 10 < flights._fi_backoff_until - time.time() <= 15
    flights._ac_cache.clear()
    flights.aircraft(30)
    assert fl.count(FI) == 1                           # still backing off: straight to adsb.lol


def test_a_refusal_backs_off_longer_than_a_rate_limit(fl):
    fl.answers[FI] = http_error(403)
    fl.answers[LOL] = {"ac": []}
    flights.aircraft(30)
    assert 55 < flights._fi_backoff_until - time.time() <= 60


def test_both_feeds_failing_raises_with_both_reasons(fl):
    fl.answers[FI] = http_error(503)
    fl.answers[LOL] = TimeoutError("timed out")
    with pytest.raises(RuntimeError, match=r"ADSB\.FI 503; ADSB\.LOL TimeoutError"):
        flights.aircraft(30)


# --- routes ----------------------------------------------------------------------------------------------------------

ADSBDB_UAL = {"response": {"flightroute": {
    "callsign_iata": "UA123", "airline": {"name": "United Airlines"},
    "origin": {"iata_code": "SFO", "icao_code": "KSFO", "name": "San Francisco International Airport",
               "municipality": "San Francisco", "country_iso_name": "US", "latitude": 37.62, "longitude": -122.38},
    "destination": {"iata_code": "ORD", "icao_code": "KORD", "name": "Chicago O'Hare International Airport",
                    "municipality": "Chicago", "country_iso_name": "US", "latitude": 41.98, "longitude": -87.9}}}}


def test_route_is_read_from_adsbdb_and_kept_six_hours(fl):
    fl.answers[DB] = ADSBDB_UAL
    r = flights.route("UAL123")
    assert r == {"airline": "United Airlines", "iata": "UA123",
                 "origin": {"iata": "SFO", "icao": "KSFO", "name": "San Francisco International Airport",
                            "city": "San Francisco", "country": "US", "lat": 37.62, "lon": -122.38},
                 "dest": {"iata": "ORD", "icao": "KORD", "name": "Chicago O'Hare International Airport",
                          "city": "Chicago", "country": "US", "lat": 41.98, "lon": -87.9}}
    assert fl.urls == ["https://api.adsbdb.com/v0/callsign/UAL123"]
    assert flights.route("UAL123") == r and fl.count(DB) == 1
    assert flights._routes["UAL123"][0] - time.time() == pytest.approx(6 * 3600, abs=5)


def test_unknown_callsign_is_remembered_as_unknown(fl):
    fl.answers[DB] = http_error(404)
    assert flights.route("ZZZ999") is None
    assert flights.route("ZZZ999") is None and fl.count(DB) == 1
    assert flights._routes["ZZZ999"][0] - time.time() == pytest.approx(3 * 3600, abs=5)


def test_a_failed_lookup_is_retried_after_ten_minutes(fl):
    fl.answers[DB] = TimeoutError()
    assert flights.route("UAL1") is None
    assert flights._routes["UAL1"][0] - time.time() == pytest.approx(600, abs=5)


def test_routes_cleans_the_list_and_asks_for_at_most_eight(fl):
    fl.answers[DB] = {"response": "unknown callsign"}
    out = flights.routes([" ual1", "UAL1", "dal2", "bad-cs", "TOOLONG123", "X", ""] + [f"SWA{i}" for i in range(10)])
    assert list(out) == ["UAL1", "DAL2"] + [f"SWA{i}" for i in range(6)]
    assert all(v is None for v in out.values())


def test_route_cache_is_pruned_when_it_grows_large(fl):
    old = time.time() - 1
    flights._routes.update({f"OLD{i}": (old, None) for i in range(5001)})
    flights._routes["KEEP1"] = (time.time() + 999, None)
    fl.answers[DB] = http_error(404)
    flights.route("NEW1")
    assert set(flights._routes) == {"KEEP1", "NEW1"}
