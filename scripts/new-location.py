#!/usr/bin/env python3
"""Create a location for wxterm from a name and coordinates; every station and label is looked up for you.

    scripts/new-location.py home 47.6062 -122.3321 --title "SEATTLE"
    scripts/new-location.py sydney -33.8688 151.2093

It writes locations/<name>.env and locations/<name>.geo.json, adds the name to locations/order (next free port) and
regenerates docker-compose.yml. Then: `docker compose up -d --build` and restart the display (or press `l`).
`home` is gitignored, so your own location never ends up in git.

What it picks (edit the .env afterwards if you know better):
  provider    nws if api.weather.gov covers the point (US), otherwise global (METAR + Open-Meteo)
  conditions  NWS: the first station NWS lists for the point; global: nearest airport METAR
  tides       nearest NOAA tide station within 60 km, else Open-Meteo's sea-level model at the nearest open water,
              else none; water temperature from the nearest NOAA sensor, else the marine model
  surf        nearest open-water point with wave data, if any
  map labels  the 14 largest towns on the map (GeoNames cities over 5,000 people, CC BY 4.0)
  transect    six points from the water (if any) through the centre and inland, named after the nearest towns
Needs only the Python standard library. Natural Earth files (~90 MB) are cached in ~/.cache/wxterm-natural-earth.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import make_geo  # noqa: E402

UA = {"User-Agent": os.environ.get("WX_UA") or "wxterm/1.0 (+https://wxterm.app)"}
SPAN_LON = 2.6       # matches the collector's WX_MAP_SPAN_LON default
MAP_ASPECT = 0.855   # map panel dot height / width at 240x67 cells


def get(url: str, timeout: int = 30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


def km(a_lat, a_lon, b_lat, b_lon) -> float:
    p = math.pi / 180
    h = (math.sin((b_lat - a_lat) * p / 2) ** 2 +
         math.cos(a_lat * p) * math.cos(b_lat * p) * math.sin((b_lon - a_lon) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


def say(*a):
    print(" ", *a)


# --- lookups ---------------------------------------------------------------------------------------------------

def timezone(lat, lon) -> str:
    d = get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
        {"latitude": lat, "longitude": lon, "timezone": "auto", "current": "temperature_2m"}))
    return d["timezone"]


def nws_point(lat, lon) -> dict | None:
    try:
        return get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")["properties"]
    except Exception:  # noqa: BLE001 - outside NWS coverage
        return None


def nws_station(pt: dict) -> str:
    feats = get(pt["observationStations"])["features"]
    for f in feats[:6]:
        sid = f["properties"]["stationIdentifier"]
        try:
            obs = get(f"https://api.weather.gov/stations/{sid}/observations/latest")["properties"]
            if (obs.get("temperature") or {}).get("value") is not None:
                return sid
        except Exception:  # noqa: BLE001 - station without recent data; try the next one
            continue
    return feats[0]["properties"]["stationIdentifier"]


def metar_station(lat, lon) -> str:
    for pad in (0.6, 1.5, 3.0):
        q = f"{lat - pad:.3f},{lon - pad:.3f},{lat + pad:.3f},{lon + pad:.3f}"
        rows = get(f"https://aviationweather.gov/api/data/metar?bbox={q}&format=json") or []
        rows = [r for r in rows if r.get("temp") is not None]
        if rows:
            return min(rows, key=lambda r: km(lat, lon, r["lat"], r["lon"]))["icaoId"]
    raise SystemExit("no METAR station within 3 degrees; set WX_OBS_STATION by hand")


def noaa_nearest(kind: str, lat, lon, max_km=60):
    d = get(f"https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type={kind}")
    best = min(d["stations"], key=lambda s: km(lat, lon, s["lat"], s["lng"]))
    dist = km(lat, lon, best["lat"], best["lng"])
    return (best["id"], best["name"].upper(), dist) if dist <= max_km else None


def marine_probe(lat, lon):
    """Nearest points with sea-level and wave data (Open-Meteo marine), probing rings around the centre."""
    pts = [(lat, lon)]
    for r in (0.15, 0.3, 0.5, 0.8):
        for b in range(0, 360, 45):
            pts.append((round(lat + r * math.cos(math.radians(b)), 3),
                        round(lon + r * math.sin(math.radians(b)) / max(0.2, math.cos(math.radians(lat))), 3)))
    q = {"latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
         "hourly": "sea_level_height_msl", "current": "wave_height", "forecast_days": 1}
    res = get("https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode(q))
    res = res if isinstance(res, list) else [res]
    tide = surf = None
    sea = []
    for p, r in zip(pts, res, strict=False):
        has_level = any(v is not None for v in (r.get("hourly") or {}).get("sea_level_height_msl") or [])
        has_waves = (r.get("current") or {}).get("wave_height") is not None
        if has_level or has_waves:
            sea.append(p)
        if tide is None and has_level:
            tide = p
        if surf is None and has_waves:
            surf = p
    return tide, surf, sea


GEONAMES = "https://download.geonames.org/export/dump/cities5000.zip"  # CC BY 4.0, geonames.org


def places():
    """(name, lat, lon, population) for every town over 5,000 people (GeoNames), cached next to Natural Earth."""
    import io
    import zipfile
    path = Path(make_geo.CACHE) / "cities5000.zip"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print("downloading GeoNames cities5000")
        urllib.request.urlretrieve(GEONAMES, path)
    with zipfile.ZipFile(path) as z:
        raw = z.read("cities5000.txt").decode()
    out = []
    for line in io.StringIO(raw):
        f = line.rstrip("\n").split("\t")
        if len(f) > 14:
            out.append((f[2] or f[1], float(f[4]), float(f[5]), int(f[14] or 0)))
    return out


def map_towns(allp, lat, lon, n=14):
    half_lon = SPAN_LON / 2 * 0.95
    half_lat = SPAN_LON * math.cos(math.radians(lat)) * MAP_ASPECT / 2 * 0.95
    cand = [p for p in allp if abs(p[1] - lat) < half_lat and abs(p[2] - lon) < half_lon
            and km(lat, lon, p[1], p[2]) > 8]
    chosen = []
    for p in sorted(cand, key=lambda p: -p[3]):
        if all(km(p[1], p[2], c[1], c[2]) > 12 for c in chosen):
            chosen.append(p)
        if len(chosen) >= n:
            break
    return [(c[0].upper(), c[1], c[2]) for c in chosen]


def transect(allp, lat, lon, sea):
    """Six points on a line from the water (if any) through the centre and inland; west to east when inland."""
    if sea:  # seaward = mean direction from the centre to the probe points that are open water
        v_lat = sum(p[0] - lat for p in sea) / len(sea)
        v_lon = sum(p[1] - lon for p in sea) / len(sea)
        norm = math.hypot(v_lat, v_lon)
        d_lat, d_lon = (-v_lat / norm, -v_lon / norm) if norm > 1e-6 else (0.0, 1.0)
        s_lat, s_lon = lat - d_lat * 0.3, lon - d_lon * 0.3
    else:
        d_lat, d_lon = 0.0, 1.0
        s_lat, s_lon = lat, lon - 0.4
    e_lat, e_lon = lat + d_lat * 1.2, lon + d_lon * 1.2
    pts = [(s_lat + (e_lat - s_lat) * t, s_lon + (e_lon - s_lon) * t) for t in (0, .2, .4, .6, .8, 1)]
    i = min(range(6), key=lambda k: km(lat, lon, *pts[k]))
    pts[i] = (lat, lon)
    near = [p for p in allp if abs(p[1] - lat) < 3 and abs(p[2] - lon) < 3]
    out, used = [], set()
    for k, (a, b) in enumerate(pts):
        cand = sorted(near, key=lambda p: km(a, b, p[1], p[2]))
        hit = next((c[0] for c in cand[:5] if km(a, b, c[1], c[2]) < 15 and c[0] not in used), None)
        if hit:
            used.add(hit)
        name = hit.upper() if hit else None
        out.append((name or ("SHORE" if k == 0 and sea else f"{round(km(pts[0][0], pts[0][1], a, b))} KM"),
                    round(a, 4), round(b, 4)))
    return out


# --- main -----------------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="short id, e.g. home or sydney (letters, digits, dashes)")
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--title", help="name shown on screen (default: the name in capitals)")
    ap.add_argument("--provider", choices=("auto", "nws", "global"), default="auto")
    ap.add_argument("--dir", default=str(ROOT / "locations"), help="locations directory")
    ap.add_argument("--no-order", action="store_true", help="don't touch locations/order or docker-compose.yml")
    ap.add_argument("--force", action="store_true", help="overwrite an existing location")
    a = ap.parse_args()
    name = a.name.lower()
    if not name.replace("-", "").isalnum():
        raise SystemExit("name: letters, digits and dashes only")
    loc = Path(a.dir)
    env_path = loc / f"{name}.env"
    if env_path.exists() and not a.force:
        raise SystemExit(f"{env_path} exists (use --force to replace it)")
    lat, lon = a.lat, a.lon
    title = (a.title or name.replace("-", " ")).upper()
    print(f"{title}  {lat:.4f}, {lon:.4f}")

    tz = timezone(lat, lon)
    say("timezone", tz)
    pt = nws_point(lat, lon) if a.provider in ("auto", "nws") else None
    provider = "nws" if pt else "global"
    if a.provider == "nws" and not pt:
        raise SystemExit("NWS doesn't cover this point")
    obs = nws_station(pt) if pt else metar_station(lat, lon)
    say("provider", provider, f"(NWS office {pt['gridId']})" if pt else "(METAR + Open-Meteo)")
    say("conditions", obs)

    tide_lines, water_pt = [], None
    model_tide, surf, sea = marine_probe(lat, lon)
    noaa = noaa_nearest("tidepredictions", lat, lon) if provider == "nws" else None
    if noaa:
        tide_lines += [f"WX_TIDE_STATION={noaa[0]}", f"WX_TIDE_NAME={noaa[1]}"]
        say("tides", f"NOAA {noaa[0]} {noaa[1]} ({noaa[2]:.0f} km)")
        wt = noaa_nearest("watertemp", lat, lon)
        if wt:
            tide_lines += [f"WX_WATER_TEMP_STATION={wt[0]}", f"WX_WATER_TEMP_NAME={wt[1]}"]
            say("water temp", f"NOAA {wt[0]} {wt[1]}")
        else:
            tide_lines += ["WX_WATER_TEMP_STATION="]
        water_pt = model_tide or surf
    elif model_tide:
        tide_lines += ["WX_TIDE_STATION=", "WX_TIDE_SOURCE=model", f"WX_TIDE_LAT={model_tide[0]}",
                       f"WX_TIDE_LON={model_tide[1]}", "WX_TIDE_NAME=COAST"]
        say("tides", f"model at {model_tide}")
        water_pt = model_tide
    else:
        tide_lines += ["WX_TIDE_STATION=", "WX_TIDE_SOURCE=none", "WX_TIDE_NAME=INLAND"]
        say("tides", "none (inland)")
    if surf:
        tide_lines += [f"WX_SURF_LAT={surf[0]}", f"WX_SURF_LON={surf[1]}", "WX_SURF_NAME=COAST"]
        say("surf", f"waves at {surf}")
    else:
        tide_lines += ["WX_SURF_LAT=", "WX_SURF_LON=", "WX_SURF_NAME="]

    allp = places()
    towns = map_towns(allp, lat, lon)
    tr = transect(allp, lat, lon, sea if water_pt else [])
    say("map labels", ", ".join(t[0] for t in towns))
    say("transect", " > ".join(t[0] for t in tr))

    fmt = ";".join
    env = "\n".join([
        f"# {title}: made by scripts/new-location.py; edit freely (keys: collector/wxcollect/config.py)",
        f"WX_NAME={title}", f"WX_LAT={lat}", f"WX_LON={lon}", f"WX_TZ={tz}", f"WX_PROVIDER={provider}",
        f"WX_OBS_STATION={obs}", *tide_lines,
        f"WX_TRANSECT_TITLE={'COAST TO INLAND' if water_pt else 'WEST TO EAST'}",
        f"WX_TRANSECT_WATER={'COAST' if water_pt else ''}",
        "WX_TRANSECT=" + fmt(f"{n}:{la}:{lo}" for n, la, lo in tr),
        "WX_TOWNS=" + fmt(f"{n}:{la:.4f}:{lo:.4f}" for n, la, lo in towns),
        f"WX_GEO=/locations/{name}.geo.json", ""])
    loc.mkdir(parents=True, exist_ok=True)
    make_geo.build(lat, lon, loc / f"{name}.geo.json", quiet=True)
    env_path.write_text(env)
    print(f"wrote {env_path} and {name}.geo.json")

    if not a.no_order:
        order = loc / "order"
        text = order.read_text() if order.exists() else ""
        rows = [ln.split("#", 1)[0].split() for ln in text.splitlines()]
        names = [r[0] for r in rows if len(r) == 2]
        if name not in names:
            port = max([int(r[1]) for r in rows if len(r) == 2] + [8090]) + 1
            lines_ = text.splitlines(keepends=True)
            i = 0
            while i < len(lines_) and lines_[i].lstrip().startswith("#"):
                i += 1
            # home always goes first so the display starts there; other places go to the end
            lines_.insert(i if name == "home" else len(lines_), f"{name} {port}\n")
            order.write_text("".join(lines_))
            print(f"added {name} to {order} on port {port}")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-compose.py")], check=True)
        print("next: docker compose up -d --build   (then press l on the display, or restart it)")


if __name__ == "__main__":
    main()
