"""The air traffic model and drawing helpers (display/wxterm/flights.py): geometry, aircraft state, routes, text,
and data tag placement. Pure functions and plain objects, no app or network."""
from __future__ import annotations

import pytest

from wxterm import flights as F

SFO = (37.6198, -122.3748)
ORD = (41.9786, -87.9048)


def ac(hex_="abc123", lat=37.70, lon=-122.40, **kw) -> dict:
    """One aircraft row the way the collector serves it."""
    return {"hex": hex_, "lat": lat, "lon": lon, "type": "adsb_icao", "flight": "UAL123", "r": "N12345",
            "t": "B738", "alt_baro": 12000, "gs": 300.0, "track": 90.0, "baro_rate": 0, "squawk": "1200",
            "emergency": "none", "seen_pos": 0.0, "seen": 0.0} | kw


def plane(now=1000.0, **kw) -> F.Plane:
    p = F.Plane(kw.get("hex_", "abc123"))
    p.update(ac(**kw), now, now)
    return p


# --- geometry ----------------------------------------------------------------------------------------------------

def test_one_degree_of_latitude_is_sixty_nautical_miles():
    assert F.dist_nm(0, 0, 1, 0) == pytest.approx(60.04, abs=0.01)
    assert F.dist_nm(*SFO, *SFO) == 0


def test_sfo_to_ord_distance():
    assert F.dist_nm(*SFO, *ORD) == pytest.approx(1600, rel=0.01)  # published great-circle distance 1,599 NM


@pytest.mark.parametrize("to, brg", [((1, 0), 0), ((0, 1), 90), ((-1, 0), 180), ((0, -1), 270)])
def test_bearing_cardinal_points(to, brg):
    assert F.bearing(0, 0, *to) == pytest.approx(brg, abs=1e-6)


@pytest.mark.parametrize("brg, nm", [(45, 100), (200, 12.5), (359, 0.5)])
def test_dest_is_the_inverse_of_dist_and_bearing(brg, nm):
    lat, lon = F.dest(*SFO, brg, nm)
    assert F.dist_nm(*SFO, lat, lon) == pytest.approx(nm, rel=1e-6)
    assert F.bearing(*SFO, lat, lon) == pytest.approx(brg, abs=0.01)


def test_cross_track_distance():
    assert F.xtrack_nm(0, 0, 0, 10, 0, 5) == pytest.approx(0, abs=1e-6)          # on the line
    assert F.xtrack_nm(0, 0, 0, 10, 1, 5) == pytest.approx(60, rel=0.01)         # a degree off it
    assert F.xtrack_nm(0, 0, 0, 10, -1, 5) == pytest.approx(60, rel=0.01)        # either side


@pytest.mark.parametrize("rng, step", [(5, 2), (10, 5), (15, 5), (25, 10), (60, 20), (100, 50), (1000, 100)])
def test_ring_step_gives_at_most_three_rings_to_the_edge(rng, step):
    assert F.ring_step(rng) == step


# --- one aircraft ------------------------------------------------------------------------------------------------

def test_plane_reads_a_feed_row():
    p = plane()
    assert (p.flight, p.reg, p.type, p.alt, p.gs, p.track, p.src) == ("UAL123", "N12345", "B738", 12000, 300.0, 90.0,
                                                                        "ADS-B")
    assert not p.ground and not p.emergency
    assert p.callsign == "UAL123"


def test_plane_on_the_ground():
    p = plane(alt_baro="ground", gs=12.0)
    assert p.ground and p.alt == 0


def test_altitude_falls_back_to_geometric_and_heading_to_true_heading():
    p = plane(alt_baro=None, alt_geom=8000, track=None, true_heading=270.0)
    assert p.alt == 8000 and p.track == 270.0


@pytest.mark.parametrize("kind, mlat, src", [("adsb_icao", False, "ADS-B"), ("mlat", True, "MLAT"),
                                              ("tisb_other", False, "TIS-B"), ("adsr_icao", False, "ADS-R"),
                                              ("mode_s", False, "MODE S"), ("", True, "MLAT")])
def test_message_source_comes_from_the_feed_type(kind, mlat, src):
    assert plane(type=kind, mlat=mlat).src == src


@pytest.mark.parametrize("fields", [{"squawk": "7700"}, {"squawk": "7600"}, {"squawk": "7500"},
                                    {"emergency": "general"}])
def test_emergencies(fields):
    assert plane(**fields).emergency


def test_callsign_falls_back_to_registration_then_hex():
    assert plane(flight="").callsign == "N12345"
    assert plane(flight="", r="").callsign == "ABC123"


def test_dead_reckoning_moves_along_track():
    p = plane(gs=360.0, track=90.0)                 # 360 kt = 0.1 NM a second
    lat, lon = p.pos(1010.0)
    assert F.dist_nm(p.lat, p.lon, lat, lon) == pytest.approx(1.0, rel=1e-3)
    assert F.bearing(p.lat, p.lon, lat, lon) == pytest.approx(90, abs=0.1)


def test_dead_reckoning_stops_after_20_seconds_in_the_air_and_6_on_the_ground():
    air = plane(gs=360.0)
    assert F.dist_nm(air.lat, air.lon, *air.pos(1060.0)) == pytest.approx(2.0, rel=1e-3)
    gnd = plane(gs=36.0, alt_baro="ground")
    assert F.dist_nm(gnd.lat, gnd.lon, *gnd.pos(1060.0)) == pytest.approx(0.06, rel=1e-3)


def test_dead_reckoning_needs_speed_and_track():
    assert plane(gs=0.5).pos(1010.0) == (37.70, -122.40)
    assert plane(track=None).pos(1010.0) == (37.70, -122.40)


def test_stale_position_is_dated_by_seen_pos():
    p = F.Plane("x")
    p.update(ac(seen_pos=4.0), 1000.0, 1000.0)
    assert p.t_pos == 996.0


def test_trail_grows_only_when_the_aircraft_moves_and_keeps_30_minutes():
    p = F.Plane("x")
    p.update(ac(lat=37.70), 1000.0, 1000.0)
    p.update(ac(lat=37.70), 1002.0, 1002.0)        # same spot: no new point
    p.update(ac(lat=37.71), 1004.0, 1004.0)
    assert len(p.trail) == 2
    p.update(ac(lat=37.72), 1000.0 + 1801, 1000.0 + 1801)
    assert [round(pt[0], 2) for pt in p.trail] == [37.71, 37.72]   # the first point aged out


# --- the traffic picture -----------------------------------------------------------------------------------------

def test_ingest_adds_aircraft_and_feed_status():
    tr = F.Traffic()
    tr.error = "OLD ERROR"
    tr.ingest({"source": "ADSB.FI", "now": 1000.0, "home": [37.77, -122.42],
               "ac": [ac("a1"), ac("a2", lat=37.8), {"hex": "nopos"}]}, 1000.0)
    assert set(tr.planes) == {"a1", "a2"}
    assert (tr.source, tr.ok_at, tr.error, tr.home) == ("ADSB.FI", 1000.0, "", (37.77, -122.42))


def test_aircraft_missing_from_the_feed_linger_25_seconds():
    tr = F.Traffic()
    tr.ingest({"ac": [ac("a1"), ac("a2")]}, 1000.0)
    tr.selected = "a2"
    tr.placed["a2"] = "NE1:2,-5"
    tr.ingest({"ac": [ac("a1")]}, 1020.0)
    assert "a2" in tr.planes                      # gone 20 s: kept
    tr.ingest({"ac": [ac("a1")]}, 1026.0)
    assert "a2" not in tr.planes and tr.selected is None and "a2" not in tr.placed


def test_reset_forgets_everything():
    tr = F.Traffic()
    tr.ingest({"source": "ADSB.FI", "ac": [ac()]}, 1000.0)
    tr.routes["UAL123"] = None
    tr.reset()
    assert not tr.planes and not tr.routes and tr.ok_at == 0 and tr.selected is None


def test_wanted_routes_are_airline_callsigns_not_yet_asked():
    tr = F.Traffic()
    ps = [plane(flight=f) for f in ("UAL1", "N12345", "DAL22", "UAL1", "SWA9", "C6556", "")]
    tr.routes["SWA9"] = None
    assert tr.wanted_routes(ps) == ["UAL1", "DAL22"]
    assert tr.wanted_routes(ps, limit=1) == ["UAL1"]


# --- routes: shown only when they fit ------------------------------------------------------------------------------

def airport(code, lat, lon):
    return {"iata": code, "icao": "K" + code, "name": code, "city": code.title(), "lat": lat, "lon": lon}


SFO_ORD = {"airline": "United Airlines", "origin": airport("SFO", *SFO), "dest": airport("ORD", *ORD)}


def on_route(frac=0.3, **kw) -> F.Plane:
    """A plane `frac` of the way from SFO to ORD, heading toward ORD unless told otherwise."""
    d = F.dist_nm(*SFO, *ORD) * frac
    lat, lon = F.dest(*SFO, F.bearing(*SFO, *ORD), d)
    kw.setdefault("track", F.bearing(lat, lon, *ORD))
    return plane(lat=lat, lon=lon, **kw)


def test_route_that_fits_is_shown():
    tr = F.Traffic()
    tr.routes["UAL123"] = SFO_ORD
    p = on_route()
    assert tr.route(p) is SFO_ORD and tr.route_text(p) == "SFO›ORD"


def test_route_far_off_the_line_is_hidden():
    tr = F.Traffic()
    tr.routes["UAL123"] = SFO_ORD
    assert tr.route(plane(lat=25.0, lon=-80.0)) is None   # over Miami


def test_route_flying_away_from_the_destination_is_hidden():
    tr = F.Traffic()
    tr.routes["UAL123"] = SFO_ORD
    p = on_route(0.5)
    p2 = on_route(0.5, track=(p.track + 180) % 360)
    assert tr.route(p) is not None
    assert tr.route(p2) is None


def test_climbing_out_of_the_destination_means_its_the_return_leg():
    tr = F.Traffic()
    tr.routes["UAL123"] = {"origin": airport("ORD", *ORD), "dest": airport("SFO", *SFO)}   # backwards
    p = plane(lat=37.70, lon=-122.30, baro_rate=2500, track=60.0)   # just departed SFO, climbing
    assert tr.route(p) is None
    tr2 = F.Traffic()
    tr2.routes["UAL123"] = SFO_ORD
    assert tr2.route(plane(lat=37.70, lon=-122.30, baro_rate=2500, track=60.0)) is not None


def test_descending_into_the_origin_is_hidden():
    tr = F.Traffic()
    tr.routes["UAL123"] = SFO_ORD
    assert tr.route(plane(lat=37.70, lon=-122.30, baro_rate=-1500, track=240.0)) is None


def test_route_without_coordinates_or_not_yet_known():
    tr = F.Traffic()
    tr.routes["UAL123"] = {"origin": {"iata": "SFO"}, "dest": airport("ORD", *ORD)}
    assert tr.route(plane()) is None
    tr.routes["UAL123"] = "pending"
    assert tr.route(plane()) is None and tr.route_text(plane()) == ""


def test_route_check_is_cached_per_flight():
    tr = F.Traffic()
    tr.routes["UAL123"] = SFO_ORD
    p = on_route()
    assert tr.route(p) is not None
    p.lat, p.lon = 25.0, -80.0                    # moving the plane later doesn't re-run the check
    assert tr.route(p) is not None
    p.flight = "UAL999"
    tr.routes["UAL999"] = SFO_ORD
    assert tr.route(p) is None                    # a new flight is checked again


# --- text ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fields, long, short", [
    ({"alt_baro": "ground"}, "GND", "GND"),
    ({"alt_baro": 35000}, "FL350", "FL350"),
    ({"alt_baro": 18000}, "FL180", "FL180"),
    ({"alt_baro": 12510}, "12500FT", "12500"),
    ({"alt_baro": 1060}, "1050FT", "1100"),
    ({"alt_baro": None, "alt_geom": None}, "-----", "---"),
])
def test_altitude_text(fields, long, short):
    p = plane(**fields)
    assert F.alt_text(p) == long and F.alt_text(p, short=True) == short


@pytest.mark.parametrize("rate, g", [(1500, "▲"), (-800, "▼"), (200, " "), (None, " ")])
def test_vertical_rate_glyph(rate, g):
    assert F.vr_glyph(plane(baro_rate=rate)) == g


def test_no_vertical_glyph_on_the_ground():
    assert F.vr_glyph(plane(alt_baro="ground", baro_rate=-900)) == " "


@pytest.mark.parametrize("track, g", [(0, "↑"), (22, "↑"), (23, "↗"), (90, "→"), (180, "↓"), (270, "←"), (315, "↖"),
                                      (359, "↑"), (None, "•")])
def test_aircraft_glyph_points_along_the_track(track, g):
    assert F.glyph(plane(track=track)) == g


def test_speed_text():
    assert F.spd(plane(gs=431.6)) == "432" and F.spd(plane(gs=None)) == "---"


# --- data tag placement ------------------------------------------------------------------------------------------

def box(pl):
    p, pc, pr, spot = pl
    return pc + spot[4], pr + spot[5], pc + spot[4] + F.BW - 1, pr + spot[5] + F.BH - 1


def overlaps(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def grid(w, h):
    return [bytearray(w) for _ in range(h)], [bytearray(w) for _ in range(h)]


@pytest.mark.parametrize("spot", F.SPOTS, ids=lambda s: s[1])
def test_every_spot_keeps_the_box_off_the_aircraft_and_ends_the_leader_at_the_box(spot):
    _, _, lead, _, bx, by, junction = spot
    x0, y0, x1, y1 = bx, by, bx + F.BW - 1, by + F.BH - 1
    inside = lambda x, y: x0 <= x <= x1 and y0 <= y <= y1  # noqa: E731
    assert not inside(0, 0)
    assert not any(inside(x, y) for x, y in lead)
    dx = (lead[0][0] > 0) - (lead[0][0] < 0)
    dy = (lead[0][1] > 0) - (lead[0][1] < 0)
    nx, ny = lead[-1][0] + dx, lead[-1][1] + dy
    assert inside(nx, ny) and (nx in (x0, x1) or ny in (y0, y1))   # the next cell is on the box border
    if junction:
        assert (junction[0], junction[1]) == (nx, ny)


def test_spots_are_tried_cheapest_first():
    costs = [s[0] for s in F.SPOTS]
    assert costs == sorted(costs)


def test_tags_fit_without_overlapping_each_other_or_aircraft():
    mw, mh = 80, 40
    occ, soft = grid(mw, mh)
    items = [(plane(hex_=f"p{i}"), x, y) for i, (x, y) in enumerate([(20, 10), (24, 12), (60, 30), (40, 20)])]
    for _, x, y in items:
        occ[y][x] = 1
    placed, failed = F.place_tags(items, occ, soft, mw, mh, {}, relaxed=False)
    assert failed == 0 and len(placed) == 4
    boxes = [box(pl) for pl in placed]
    for i, b in enumerate(boxes):
        assert 0 <= b[0] and 0 <= b[1] and b[2] < mw and b[3] < mh
        assert not any(overlaps(b, o) for o in boxes[i + 1:])
        assert not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for _, x, y in items)


def test_a_tag_keeps_last_frames_spot_while_its_free():
    mw, mh = 80, 40
    occ, soft = grid(mw, mh)
    p = plane()
    keep = "SW3:-21,4"
    assert keep in F.SPOT_BY_KEY
    placed, _ = F.place_tags([(p, 40, 20)], occ, soft, mw, mh, {p.hex: keep}, relaxed=False)
    assert placed[0][3][1] == keep


def test_tags_avoid_map_labels_when_they_can():
    mw, mh = 80, 40
    occ, soft = grid(mw, mh)
    first, _ = F.place_tags([(plane(), 40, 20)], [r[:] for r in occ], soft, mw, mh, {}, relaxed=False)
    x0, y0, x1, y1 = box(first[0])
    for y in range(y0, y1 + 1):              # put a label where the best spot was
        soft[y][x0:x1 + 1] = b"\x01" * (x1 - x0 + 1)
    again, _ = F.place_tags([(plane(), 40, 20)], occ, soft, mw, mh, {}, relaxed=False)
    assert box(again[0]) != (x0, y0, x1, y1)


def test_no_room_counts_a_failure_unless_relaxed():
    mw, mh = 30, 12
    occ, soft = grid(mw, mh)
    for row in occ:                          # everything taken except the aircraft's cell
        row[:] = b"\x01" * mw
    placed, failed = F.place_tags([(plane(), 15, 6)], [r[:] for r in occ], soft, mw, mh, {}, relaxed=False)
    assert (placed, failed) == ([], 1)
    placed, failed = F.place_tags([(plane(), 15, 6)], occ, soft, mw, mh, {}, relaxed=True)
    assert failed == 0 and len(placed) == 1   # the selected aircraft's tag always shows


def test_tags_stay_on_the_map_near_its_edges():
    mw, mh = 60, 30
    for x, y in [(0, 0), (mw - 1, 0), (0, mh - 1), (mw - 1, mh - 1)]:
        occ, soft = grid(mw, mh)
        occ[y][x] = 1
        placed, failed = F.place_tags([(plane(), x, y)], occ, soft, mw, mh, {}, relaxed=False)
        b = box(placed[0])
        assert failed == 0 and 0 <= b[0] and 0 <= b[1] and b[2] < mw and b[3] < mh


# --- the still map: labels -----------------------------------------------------------------------------------------

def _base(airports, mark="HOME"):
    from wxterm.themes import THEMES
    t = THEMES[0]
    geo = {"airports": airports, "runways": [], "towns": []}
    P = lambda lat, lon: (lon, lat)  # noqa: E731 - test map: x = lon, y = lat, in dots
    b, soft, rain = F.MapPanel._build_base(None, t, 40, 12, P, 2.0, 25, geo, None, F.WX_DENS, False, mark)
    rows = ["".join(b.chars[y][x] if b.text[y][x] else " " for x in range(b.w)) for y in range(b.h)]
    return rows, rain


def test_neighbouring_airport_labels_never_run_together():
    # NZY's label takes cells 9-11 and SAN's would start at 12: SAN moves rather than reading NZYSAN
    rows, _ = _base([["NZY", "North Island", 8, 20, "L"], ["SAN", "San Diego", 8, 26, "L"]])
    joined = "\n".join(rows)
    assert "NZY" in joined and "SAN" in joined and "NZYSAN" not in joined
    assert not any("NZY" in r and "SAN" in r and r.index("SAN") - r.index("NZY") < 4 for r in rows)


def test_home_is_labelled_on_the_map():
    rows, rain = _base([], mark="TESTVILLE")
    assert any("⌂ TESTVILLE" in r for r in rows) and rain is False
