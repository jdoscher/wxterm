#!/usr/bin/env python3
"""Build locations/<name>.geo.json (coast, borders, state lines, lakes, major roads, airports) around a map centre.

Usage: scripts/make_geo.py --out locations/NAME.geo.json [--pad-lon 2.6 --pad-lat 1.8] [--cache DIR] -- LAT LON
       scripts/make_geo.py --add-airports locations/NAME.geo.json [...]   (update older files in place)
Downloads Natural Earth 10m GeoJSON (public domain, ~90 MB total) and the OurAirports CSVs (public domain, ~16 MB)
into the cache dir once, then clips them. scripts/new-location.py calls build() for you.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.request
from pathlib import Path

NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}.geojson"
LAYERS = {"coast": "ne_10m_coastline", "border": "ne_10m_admin_0_boundary_lines_land",
          "state": "ne_10m_admin_1_states_provinces_lines", "lakes": "ne_10m_lakes", "roads": "ne_10m_roads"}
CACHE = os.environ.get("NE_CACHE") or str(Path.home() / ".cache" / "wxterm-natural-earth")
OURAIRPORTS = "https://davidmegginson.github.io/ourairports-data/{}.csv"


def lines(geom):
    t, c = geom["type"], geom["coordinates"]
    return {"LineString": lambda: [c], "MultiLineString": lambda: c, "Polygon": lambda: c,
            "MultiPolygon": lambda: [r for p in c for r in p]}.get(t, lambda: [])()


def layer(name: str, cache: str = CACHE) -> list[dict]:
    """Features of one Natural Earth 10m layer, downloaded once into the cache."""
    path = Path(cache) / f"{name}.geojson"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print("downloading", name)
        urllib.request.urlretrieve(NE.format(name), path)
    return json.loads(path.read_text())["features"]


def _csv(name: str, cache: str = CACHE) -> list[dict]:
    """Rows of one OurAirports CSV, downloaded once into the cache."""
    path = Path(cache) / f"ourairports-{name}.csv"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print("downloading", name, "(OurAirports)")
        urllib.request.urlretrieve(OURAIRPORTS.format(name), path)
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def airports(B, cache: str = CACHE) -> dict:
    """Large and medium airports inside box B (lat0, lat1, lon0, lon1), and their runways as end-to-end lines.

    airports: [code, name, lat, lon, size] with size L (large) or M (medium) and code the IATA code if there is one.
    runways:  [[lon, lat], [lon, lat]]. Runways without surveyed ends (common outside the US) are left out.
    """
    keep: dict[str, list] = {}
    for r in _csv("airports", cache):
        if r["type"] not in ("large_airport", "medium_airport"):
            continue
        try:
            lat, lon = float(r["latitude_deg"]), float(r["longitude_deg"])
        except ValueError:
            continue
        if B[0] <= lat <= B[1] and B[2] <= lon <= B[3]:
            code = r["iata_code"] or r.get("icao_code") or r["gps_code"] or r["ident"]
            keep[r["ident"]] = [code, r["name"], round(lat, 4), round(lon, 4), "L" if r["type"][0] == "l" else "M"]
    runways = []
    for r in _csv("runways", cache):
        if r["airport_ident"] not in keep or r["closed"] == "1" or "WATER" in r["surface"].upper():
            continue
        try:
            ends = [[round(float(r[f"{e}_longitude_deg"]), 5), round(float(r[f"{e}_latitude_deg"]), 5)]
                    for e in ("le", "he")]
        except ValueError:
            continue
        runways.append(ends)
    return {"airports": sorted(keep.values(), key=lambda a: (a[4], a[0])), "runways": runways,
            "airports_source": "OurAirports (public domain)"}


def add_airports(path: str | Path, cache: str = CACHE) -> None:
    """Add airports and runways to an existing geo.json (made before they were part of it), keeping its box."""
    path = Path(path)
    geo = json.loads(path.read_text())
    geo.update(airports(geo["bbox"], cache))
    path.write_text(json.dumps(geo, separators=(",", ":")))
    print(f"{path}: {len(geo['airports'])} airports, {len(geo['runways'])} runways")


def _keep_all(_props) -> bool:
    return True


def _major_road(props) -> bool:
    return props.get("type") == "Major Highway" or props.get("expressway") == 1


def build(lat: float, lon: float, out: str | Path, pad_lon: float = 2.6, pad_lat: float = 1.8,
          cache: str = CACHE, quiet: bool = False) -> Path:
    B = (lat - pad_lat, lat + pad_lat, lon - pad_lon, lon + pad_lon)

    def inb(p):
        return B[0] <= p[1] <= B[1] and B[2] <= p[0] <= B[3]

    def clip(feats, keep):
        res = []
        for f in feats:
            if not f.get("geometry") or not keep(f["properties"]):
                continue
            for ln in lines(f["geometry"]):
                cur = []
                for i, p in enumerate(ln):  # keep a point if it or a neighbour is inside, so lines reach the edge
                    if inb(p) or (i and inb(ln[i - 1])) or (i + 1 < len(ln) and inb(ln[i + 1])):
                        cur.append([round(p[0], 4), round(p[1], 4)])
                    elif cur:
                        if len(cur) > 1:
                            res.append(cur)
                        cur = []
                if len(cur) > 1:
                    res.append(cur)
        return res

    geo = {"bbox": B, "source": "Natural Earth 10m (public domain)"}
    for key, name in LAYERS.items():
        geo[key] = clip(layer(name, cache), _major_road if key == "roads" else _keep_all)
        if not quiet:
            print(f"{key}: {len(geo[key])} lines, {sum(map(len, geo[key]))} points")
    geo.update(airports(B, cache))
    if not quiet:
        print(f"airports: {len(geo['airports'])}, runways: {len(geo['runways'])}")
    out = Path(out)
    out.write_text(json.dumps(geo, separators=(",", ":")))
    if not quiet:
        print("wrote", out, out.stat().st_size, "bytes")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float, nargs="?")
    ap.add_argument("lon", type=float, nargs="?")
    ap.add_argument("--pad-lon", type=float, default=2.6)
    ap.add_argument("--pad-lat", type=float, default=1.8)
    ap.add_argument("--out")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--add-airports", nargs="+", metavar="GEO_JSON", help="add airports and runways to these files")
    a = ap.parse_args()
    if a.add_airports:
        for p in a.add_airports:
            add_airports(p, a.cache)
        return
    if a.lat is None or a.lon is None or not a.out:
        ap.error("LAT, LON and --out are required (or use --add-airports)")
    build(a.lat, a.lon, a.out, a.pad_lon, a.pad_lat, a.cache)


if __name__ == "__main__":
    main()
