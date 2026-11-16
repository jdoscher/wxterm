#!/usr/bin/env python3
"""Build locations/<name>.geo.json (coast, borders, state lines, lakes, major roads) around a map centre.

Usage: scripts/make_geo.py --out locations/NAME.geo.json [--pad-lon 2.6 --pad-lat 1.8] [--cache DIR] -- LAT LON
Downloads Natural Earth 10m GeoJSON (public domain, ~90 MB total) into the cache dir once, then clips it.
scripts/new-location.py calls build() for you.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}.geojson"
LAYERS = {"coast": "ne_10m_coastline", "border": "ne_10m_admin_0_boundary_lines_land",
          "state": "ne_10m_admin_1_states_provinces_lines", "lakes": "ne_10m_lakes", "roads": "ne_10m_roads"}
CACHE = os.environ.get("NE_CACHE") or str(Path.home() / ".cache" / "wxterm-natural-earth")


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
    out = Path(out)
    out.write_text(json.dumps(geo, separators=(",", ":")))
    if not quiet:
        print("wrote", out, out.stat().st_size, "bytes")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--pad-lon", type=float, default=2.6)
    ap.add_argument("--pad-lat", type=float, default=1.8)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=CACHE)
    a = ap.parse_args()
    build(a.lat, a.lon, a.out, a.pad_lon, a.pad_lat, a.cache)


if __name__ == "__main__":
    main()
