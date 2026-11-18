"""The spacecraft layer's arithmetic (display/wxterm/space.py), checked against Skyfield (an independent, precise
library) for one real ISS element set. No network: the elements are pasted in below."""
from __future__ import annotations

import pytest

from wxterm import space as S

ISS = {"OBJECT_NAME": "ISS (ZARYA)", "OBJECT_ID": "1998-067A", "EPOCH": "2026-11-17T12:40:23.465568",
       "MEAN_MOTION": 15.4943533, "ECCENTRICITY": 0.00073793, "INCLINATION": 51.6318, "RA_OF_ASC_NODE": 261.2656,
       "ARG_OF_PERICENTER": 36.1207, "MEAN_ANOMALY": 324.0279, "EPHEMERIS_TYPE": 0, "CLASSIFICATION_TYPE": "U",
       "NORAD_CAT_ID": 25544, "ELEMENT_SET_NO": 999, "REV_AT_EPOCH": 59552, "BSTAR": 0.00019072706,
       "MEAN_MOTION_DOT": 0.00010237, "MEAN_MOTION_DDOT": 0}
T = 1794922823                      # an hour after the epoch
SKYFIELD_AT_T = (-38.7654, -141.4529, 435.68)
SF = (37.7749, -122.4194)
# Skyfield's culmination times and peak elevations over SF for the next two days, all passes above 10°
SKYFIELD_PEAKS = [(1794958963, 46.8), (1794964780, 21.1), (1794982394, 31.8), (1794988195, 25.0),
                  (1795042498, 25.3), (1795048299, 31.0), (1795065914, 21.6), (1795071729, 45.9)]


@pytest.fixture(scope="module")
def sky():
    s = S.Sky()
    s.load("stations", [ISS, ISS | {"NORAD_CAT_ID": 99999, "OBJECT_NAME": "STARLINK-31234"}], T)
    return s


def test_names_and_stations(sky):
    assert [c.name for c in sky.crafts] == ["ISS", "STARLINK 31234"] and sky.stations == [0]


def test_a_group_reload_keeps_one_copy_of_each_object(sky):
    s = S.Sky()
    s.load("stations", [ISS], T)
    s.load("visual", [ISS], T)
    assert len(s) == 1


def test_position_matches_skyfield(sky):
    r, ok = sky.propagate([0], [T])
    lat, lon, alt = S.geodetic(r, [[T]])
    assert ok.all()
    assert lat[0, 0] == pytest.approx(SKYFIELD_AT_T[0], abs=0.02)
    assert lon[0, 0] == pytest.approx(SKYFIELD_AT_T[1], abs=0.02)
    assert alt[0, 0] == pytest.approx(SKYFIELD_AT_T[2], abs=1.0)


def test_passes_match_skyfield(sky):
    passes = [p for p in sky.passes(SF, T) if p.name == "ISS"]
    assert len(passes) == len(SKYFIELD_PEAKS)
    for p, (peak, el) in zip(passes, SKYFIELD_PEAKS):
        assert abs(p.peak - peak) <= 15 and p.max_el == pytest.approx(el, abs=1.0)
        assert p.rise < p.peak < p.set


def test_on_the_map_and_candidates(sky):
    lat, lon, _ = SKYFIELD_AT_T
    box = (lon - 1, lon + 1, lat - 1, lat + 1)
    assert 0 in sky.candidates(box, T - 30)
    now = sky.now([0], T, box)
    assert len(now) == 1 and now[0][0].name == "ISS" and len(now[0][4]) > 30   # with a ground track
    far = (lon + 40, lon + 42, lat - 1, lat + 1)
    assert sky.now([0], T, far) == [] and 0 not in sky.candidates(far, T)


def test_next_over_map_is_soon_for_a_box_it_is_about_to_cross(sky):
    lat, lon, _ = SKYFIELD_AT_T
    nxt = sky.next_over_map((lon - 1, lon + 1, lat - 1, lat + 1), T - 300)
    assert nxt and nxt[0] == "ISS" and T - 300 <= nxt[1] <= T


def test_until_text():
    assert S.fmt_until(1000 + 125, 1000) == "2 MIN"
    assert S.fmt_until(1000 + 3 * 3600 + 600, 1000) == "3H 10M"
    assert S.fmt_until(1000 + 2 * 86400 + 5 * 3600, 1000) == "2D 5H"


def test_docked_modules_fold_into_their_station():
    s = S.Sky()
    s.load("stations", [ISS, ISS | {"NORAD_CAT_ID": 49044, "OBJECT_NAME": "ISS (NAUKA)"}], T)
    lat, lon, _ = SKYFIELD_AT_T
    now = s.now([0, 1], T, (lon - 1, lon + 1, lat - 1, lat + 1))
    assert [c.name for c, *_ in now] == ["ISS"]


def test_display_asks_again_soon_for_a_missing_group(monkeypatch):
    """A collector that hasn't fetched Starlink yet: the display retries in 10 minutes, not at the 3-hourly round."""
    from wxterm import api
    from wxterm import app as appmod

    app = appmod.WxApp()
    have = {"stations": {"records": [], "fetched": None}}
    monkeypatch.setattr(api, "get", lambda base, path, timeout=None: have.get(path.split("=")[1], {}))
    monkeypatch.setattr(app, "run_worker", lambda fn, **kw: fn())
    monkeypatch.setattr(app, "call_from_thread", lambda fn, *a: fn(*a))
    timers = []
    monkeypatch.setattr(app, "set_timer", lambda secs, cb: timers.append((secs, cb)))
    app._space_fetch()
    assert timers == [(600, app._space_fetch)]
    app.sky.loaded.update(stations=1.0, visual=1.0, starlink=1.0)   # everything in: no retry
    timers.clear()
    app._space_fetch()
    assert timers == []


def test_celestrak_tags_come_off_names():
    assert S.short_name(12345, "STARLINK-11146 [DTC]") == "STARLINK 11146"


def _draw_one(name, station, soft, mw=60, mh=5):
    """One spacecraft at map cell (30, 2) through MapPanel._spacecraft; the map row it lands on, as text."""
    from types import SimpleNamespace as NS

    from wxterm import flights as F
    from wxterm.canvas import Canvas
    craft = NS(name=name, station=station)
    sky = NS(cand=[0], now=lambda idx, now, bbox: [(craft, 0.0, 0.0, 420.0, [])])
    panel = NS(app=NS(sky=sky, geo={"home": None}), bbox=lambda home: (0, 1, 0, 1), _seg=None,
               _clear=F.MapPanel._clear)
    c = Canvas(mw + 2, mh + 2)
    F.MapPanel._spacecraft(panel, c, NS(accent="a", fg="f", dim="d"), mw, mh, lambda la, lo: (60, 8), 0, soft)
    return "".join(c.chars[3])[1:mw + 1]


def test_spacecraft_labels_steer_around_town_names():
    town = "NEW YORK CITY"
    soft = [bytearray(60) for _ in range(5)]
    soft[2][32:32 + len(town)] = b"\x01" * len(town)        # a town name just right of the marker
    row = _draw_one("STARLINK 11146", False, soft)
    assert row.index("◇") == 30 and "STARLINK 11146 420 KM" in row[:30]   # it went left instead
    soft[2][:30] = b"\x01" * 30                               # both sides taken: marker only
    assert _draw_one("STARLINK 11146", False, soft).strip() == "◇"
    assert "ISS 420 KM" in _draw_one("ISS", True, soft)          # a station keeps its label regardless
