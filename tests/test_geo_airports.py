"""Airports and runways in the location map files (scripts/make_geo.py), built from small OurAirports CSV fixtures,
plus a check of the committed demo cities."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

import make_geo

ROOT = Path(__file__).resolve().parent.parent
BOX = (37.0, 38.5, -123.0, -121.5)          # lat0, lat1, lon0, lon1 around the Bay Area

AIRPORTS = [  # ident, type, name, lat, lon, iata, icao, gps
    ("KSFO", "large_airport", "San Francisco International Airport", "37.6198", "-122.3748", "SFO", "KSFO", "KSFO"),
    ("KHWD", "medium_airport", "Hayward Executive Airport", "37.6592", "-122.1217", "HWD", "KHWD", "KHWD"),
    ("KXYZ", "medium_airport", "No IATA Field", "37.5", "-122.0", "", "KXYZ", "KXYZ"),
    ("CA99", "medium_airport", "GPS Only Field", "37.4", "-122.1", "", "", "CA99"),
    ("KPAO", "small_airport", "Palo Alto Airport", "37.4611", "-122.1150", "PAO", "KPAO", "KPAO"),
    ("CA01", "heliport", "Some Helipad", "37.7", "-122.4", "", "", "CA01"),
    ("KLAX", "large_airport", "Los Angeles International Airport", "33.9425", "-118.4081", "LAX", "KLAX", "KLAX"),
    ("BAD1", "medium_airport", "Broken Row", "", "-122.2", "BAD", "", ""),
]
RUNWAYS = [  # airport, closed, surface, le lat, le lon, he lat, he lon
    ("KSFO", "0", "ASP", "37.6117", "-122.3934", "37.6278", "-122.3577"),
    ("KSFO", "1", "ASP", "37.60", "-122.38", "37.62", "-122.36"),          # closed
    ("KSFO", "0", "WATER", "37.60", "-122.40", "37.61", "-122.39"),         # seaplane lane
    ("KHWD", "0", "ASP", "", "", "", ""),                                   # ends not surveyed
    ("KPAO", "0", "ASP", "37.457", "-122.114", "37.465", "-122.117"),      # small airport: not kept
    ("KHWD", "0", "ASPH", "37.6573", "-122.1252", "37.6612", "-122.1181"),
]


@pytest.fixture
def cache(tmp_path):
    with (tmp_path / "ourairports-airports.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ident", "type", "name", "latitude_deg", "longitude_deg", "iata_code", "icao_code", "gps_code"])
        w.writerows(AIRPORTS)
    with (tmp_path / "ourairports-runways.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["airport_ident", "closed", "surface", "le_latitude_deg", "le_longitude_deg",
                    "he_latitude_deg", "he_longitude_deg"])
        w.writerows(RUNWAYS)
    return str(tmp_path)


def test_large_and_medium_airports_in_the_box(cache):
    got = make_geo.airports(BOX, cache)
    assert got["airports"] == [
        ["SFO", "San Francisco International Airport", 37.6198, -122.3748, "L"],
        ["CA99", "GPS Only Field", 37.4, -122.1, "M"],      # no IATA or ICAO code: the GPS code
        ["HWD", "Hayward Executive Airport", 37.6592, -122.1217, "M"],
        ["KXYZ", "No IATA Field", 37.5, -122.0, "M"],       # no IATA code: the ICAO code
    ]
    assert got["airports_source"].startswith("OurAirports")


def test_runways_are_open_paved_and_surveyed(cache):
    assert make_geo.airports(BOX, cache)["runways"] == [
        [[-122.3934, 37.6117], [-122.3577, 37.6278]],
        [[-122.1252, 37.6573], [-122.1181, 37.6612]],
    ]


def test_add_airports_updates_an_old_file_in_place(cache, tmp_path):
    path = tmp_path / "old.geo.json"
    old = {"bbox": list(BOX), "source": "Natural Earth", "coast": [[[-122.5, 37.7], [-122.4, 37.8]]], "roads": []}
    path.write_text(json.dumps(old))
    make_geo.add_airports(path, cache)
    new = json.loads(path.read_text())
    assert {k: new[k] for k in old} == old
    assert [a[0] for a in new["airports"]] == ["SFO", "CA99", "HWD", "KXYZ"] and len(new["runways"]) == 2


def test_command_line(cache, tmp_path):
    path = tmp_path / "x.geo.json"
    path.write_text(json.dumps({"bbox": list(BOX)}))
    script = str(ROOT / "scripts" / "make_geo.py")
    r = subprocess.run([sys.executable, script, "--add-airports", str(path), "--cache", cache],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "4 airports, 2 runways" in r.stdout
    r = subprocess.run([sys.executable, script, "--cache", cache], capture_output=True, text=True)
    assert r.returncode == 2 and "--add-airports" in r.stderr


@pytest.mark.parametrize("path", sorted((ROOT / "locations").glob("*.geo.json")), ids=lambda p: p.stem)
def test_demo_cities_have_airports_inside_their_map(path):
    g = json.loads(path.read_text())
    lat0, lat1, lon0, lon1 = g["bbox"]
    assert g["airports"] and any(a[4] == "L" for a in g["airports"])
    for code, name, lat, lon, size in g["airports"]:
        assert code and name and size in ("L", "M") and lat0 <= lat <= lat1 and lon0 <= lon <= lon1
    assert all(len(r) == 2 for r in g["runways"])
