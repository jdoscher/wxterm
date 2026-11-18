"""Earthquake and wildfire layers: the collector's parsers and merge (collector/wxcollect/hazards.py) on small,
real-shaped samples of each feed, and the display's filters and drawing (display/wxterm/hazards.py). No network."""
from __future__ import annotations

import time

from wxcollect import hazards as C
from wxterm import hazards as H
from wxterm.canvas import Canvas
from wxterm.themes import THEMES

NOW = 1795000000.0

USGS = {"features": [
    {"id": "ci1", "properties": {"mag": 3.4, "time": (NOW - 600) * 1000, "place": "5 km N of Ridgecrest, CA",
                                 "type": "earthquake", "magType": "ml", "felt": 12, "tsunami": 0, "alert": None},
     "geometry": {"coordinates": [-117.67, 35.67, 8.2]}},
    {"id": "nc2", "properties": {"mag": 1.1, "time": (NOW - 7200) * 1000, "place": "The Geysers, CA",
                                 "type": "earthquake", "magType": "md"},
     "geometry": {"coordinates": [-122.8, 38.8, 2.0]}},
    {"id": "qb", "properties": {"mag": 2.0, "time": NOW * 1000, "place": "mine", "type": "explosion"},
     "geometry": {"coordinates": [-118, 34, 0]}},
    {"id": "nomag", "properties": {"mag": None, "time": NOW * 1000, "type": "earthquake"},
     "geometry": {"coordinates": [-118, 34, 0]}},
]}

EMSC = {"features": [
    # the same Ridgecrest quake, 20 s and a few km off: dropped as a duplicate
    {"properties": {"unid": "20261118_1", "time": "2026-11-18T09:56:00.0Z", "lat": 35.70, "lon": -117.70, "mag": 3.3,
                    "magtype": "ml", "depth": 9, "flynn_region": "SOUTHERN CALIFORNIA", "evtype": "ke"}},
    {"properties": {"unid": "20261118_2", "time": "2026-11-18T08:00:00.0Z", "lat": 43.1, "lon": 13.0, "mag": 2.8,
                    "magtype": "ml", "depth": 10, "flynn_region": "CENTRAL ITALY", "evtype": "ke"}},
    {"properties": {"unid": "20261118_3", "time": "2026-11-18T08:00:00.0Z", "lat": 43.1, "lon": 13.0, "mag": 2.0,
                    "magtype": "ml", "depth": 0, "flynn_region": "CENTRAL ITALY", "evtype": "qb"}},
]}


def test_usgs_keeps_earthquakes_with_a_magnitude():
    qs = C.parse_usgs(USGS)
    assert [q["id"] for q in qs] == ["ci1", "nc2"]
    q = qs[0]
    assert (q["lat"], q["lon"], q["depth_km"], q["mag"], q["felt"], q["src"]) == (35.67, -117.67, 8.2, 3.4, 12, "USGS")
    assert q["t"] == NOW - 600


def test_emsc_drops_blasts_and_names_regions():
    qs = C.parse_emsc(EMSC)
    assert len(qs) == 2 and qs[1]["place"] == "Central Italy" and qs[0]["id"] == "emsc-20261118_1"


def test_merge_drops_emsc_duplicates_of_usgs():
    u = C.parse_usgs(USGS)
    e = C.parse_emsc(EMSC)
    e[0]["t"] = NOW - 580                      # 20 s after the USGS origin time
    m = C.merge_quakes(u, e, NOW)
    assert [q["id"] for q in m] == ["ci1", "nc2", "emsc-20261118_2"] or \
        sorted(q["id"] for q in m) == ["ci1", "emsc-20261118_2", "nc2"]
    assert all(m[i]["t"] >= m[i + 1]["t"] for i in range(len(m) - 1))   # newest first
    old = dict(u[0], id="old", t=NOW - 8 * 86400)
    assert "old" not in [q["id"] for q in C.merge_quakes([*u, old], [], NOW)]


def test_nifc_incidents_skip_long_contained_fires():
    feats = [
        {"attributes": {"IncidentName": " Timber ", "IncidentSize": 25350.0, "PercentContained": 100,
                        "ModifiedOnDateTime_dt": (NOW - 3600) * 1000, "FireDiscoveryDateTime": (NOW - 9e5) * 1000,
                        "IrwinID": "x1"}, "geometry": {"x": -121.5, "y": 39.9}},
        {"attributes": {"IncidentName": "Old", "IncidentSize": 50, "PercentContained": 100,
                        "ModifiedOnDateTime_dt": (NOW - 10 * 86400) * 1000}, "geometry": {"x": -120, "y": 38}},
        {"attributes": {"IncidentName": "New", "IncidentSize": 0.1, "PercentContained": None,
                        "ModifiedOnDateTime_dt": NOW * 1000}, "geometry": {"x": -119, "y": 37}},
        {"attributes": {"IncidentName": "NoGeom"}, "geometry": None},
    ]
    fs = C.parse_nifc_incidents(feats, NOW)
    assert [f["name"] for f in fs] == ["TIMBER", "NEW"]
    assert fs[0]["acres"] == 25350.0 and fs[0]["contained"] == 100 and fs[0]["src"] == "NIFC"


def test_nifc_perimeters_match_incidents():
    feats = [{"attributes": {"attr_IncidentName": "Timber", "poly_GISAcres": 25350.4, "attr_PercentContained": 100},
              "geometry": {"rings": [[[-121.6, 39.9], [-121.4, 39.9], [-121.4, 40.0], [-121.6, 39.9]]]}},
             {"attributes": {"attr_IncidentName": "Elsewhere"}, "geometry": {"rings": [[[0, 0], [1, 0], [1, 1]]]}}]
    ps = C.parse_nifc_perimeters(feats, {"TIMBER"})
    assert len(ps) == 1 and ps[0]["name"] == "TIMBER" and len(ps[0]["rings"][0]) == 4


def test_gdacs_current_non_us_wildfires():
    doc = {"features": [
        {"properties": {"eventtype": "WF", "eventid": 1, "episodeid": 2, "iscurrent": "true", "country": "Australia",
                        "alertlevel": "Green", "fromdate": "2026-11-10T00:00:00", "todate": "2026-11-17T00:00:00",
                        "severitydata": {"severity": 15726.0, "severityunit": "ha"}},
         "geometry": {"coordinates": [147.0, -36.0]}},
        {"properties": {"eventtype": "WF", "eventid": 3, "iscurrent": "true", "country": "United States"},
         "geometry": {"coordinates": [-120, 38]}},
        {"properties": {"eventtype": "WF", "eventid": 4, "iscurrent": "false", "country": "Chile"},
         "geometry": {"coordinates": [-70, -33]}},
    ]}
    fs = C.parse_gdacs(doc)
    assert len(fs) == 1 and fs[0]["ha"] == 15726.0 and fs[0]["acres"] == round(15726 * 2.47105)
    assert fs[0]["alert"] == "GREEN" and H.fire_label(fs[0]) == "WILDFIRE 15,726 HA"


def test_cache_serves_the_old_copy_when_a_source_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(C.config, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(C, "_mem", {})
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if "earthquake.usgs.gov" in url:
            import json
            return json.dumps(USGS).encode()
        raise OSError("down")

    monkeypatch.setattr(C, "get", fake_get)
    a = C.quakes(NOW)
    assert a["sources"] == ["USGS"] and "emsc" in a["error"] and len(a["quakes"]) == 2
    n = len(calls)
    assert C.quakes(NOW + 10)["quakes"] == a["quakes"] and len(calls) == n   # within the TTL: no fetch
    monkeypatch.setattr(C, "get", lambda url, **kw: (_ for _ in ()).throw(OSError("all down")))
    b = C.quakes(NOW + C.QUAKE_TTL + 1)
    assert b["sources"] == [] and len(b["quakes"]) == 2 and b["fetched"] == NOW   # the last good copy


def test_display_filters_small_old_quakes():
    qs = [{"t": NOW - 600, "mag": 1.6}, {"t": NOW - 2 * 86400, "mag": 2.0}, {"t": NOW - 2 * 86400, "mag": 2.6},
          {"t": NOW - 600, "mag": 1.0}, {"t": NOW - 9 * 86400, "mag": 6.0}]
    assert [q["mag"] for q in H.shown_quakes(qs, NOW, 1.5)] == [1.6, 2.6]
    assert H.ring_dots(5) > H.ring_dots(3) > H.ring_dots(1)


def test_draw_quakes_and_fires_on_a_canvas():
    t = THEMES[0]
    c = Canvas(60, 20)

    def proj(lon, lat):      # a plain box: lon -10..10, lat -5..5 over the canvas's 120 x 80 dots
        return (lon + 10) * 6, (5 - lat) * 8

    quakes = [{"t": NOW - 600, "mag": 4.2, "lat": 0, "lon": 0, "place": "HERE"},
              {"t": NOW - 3 * 86400, "mag": 2.7, "lat": 2, "lon": -6, "place": "OLD"},
              {"t": NOW - 600, "mag": 3.0, "lat": 40, "lon": 40, "place": "OFF MAP"}]
    assert H.draw_quakes(c, t, quakes, proj, NOW, 1.5) == 2
    text = "\n".join("".join(ch or " " for ch in row) for row in c.chars)
    assert "M4.2 10M AGO" in text and "M2.7" in text
    fires = [{"name": "TIMBER", "lat": -3, "lon": 6, "acres": 25350, "contained": 40, "src": "NIFC"}]
    perims = [{"name": "TIMBER", "rings": [[[5, -2], [7, -2], [7, -4], [5, -4]]], "contained": 40}]
    assert H.draw_fires(c, t, fires, perims, proj) == 1
    text = "\n".join("".join(ch or " " for ch in row) for row in c.chars)
    assert "▲" in text and "TIMBER 25,350 AC 40%" in text
    assert any(any(row) for row in c.dots)


def test_near_and_labels():
    home = (37.77, -122.42)
    rows = [{"lat": 37.8, "lon": -122.3}, {"lat": 40.0, "lon": -100.0}]
    n = H.near(rows, home, 300)
    assert len(n) == 1 and n[0][0] < 10
    assert H.fire_label({"name": "NEW", "acres": 0.1, "contained": None, "src": "NIFC"}) == "NEW"
    assert H.ago(time.time() - 7200, time.time()) == "2H"
