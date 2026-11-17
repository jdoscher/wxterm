"""Radar: RainViewer composite frames (primary) or the IEM NEXRAD n0q composite (fallback), reduced to level grids.

Levels per pixel: 0 none, 1 = 10-19 dBZ, 2 = 20-29, 3 = 30-39, 4 = 40-49, 5 = 50+; snow adds 10.
RainViewer's free API serves zoom <= 7 and only colour scheme 2 (Universal Blue), so the scheme table
(assets/rainviewer_scheme2.json, from rainviewer.com/api/color-schemes.html) is inverted back to dBZ.
"""
from __future__ import annotations

import io
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from . import config
from .net import get, get_json

Z, TILE = 7, 512
WORLD = (1 << Z) * TILE
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def level_for(dbz: float, snow: bool) -> int:
    if dbz < 10:
        return 0
    lv = min(5, 1 + int((dbz - 10) // 10))
    return lv + 10 if snow else lv


def _load_scheme() -> dict[tuple, int]:
    d = json.loads((ASSETS / "rainviewer_scheme2.json").read_text())
    lut: dict[tuple, int] = {}
    for kind in ("rain", "snow"):
        for dbz, hx in d[kind]:
            rgba = tuple(int(hx[i:i + 2], 16) for i in (1, 3, 5, 7))
            if rgba[3] == 0:
                continue
            lut.setdefault(rgba, level_for(dbz, kind == "snow"))
    return lut


LUT = _load_scheme()
_near: dict[tuple, int] = {}


def _lookup(rgba: tuple) -> int:
    v = LUT.get(rgba)
    if v is not None:
        return v
    if rgba not in _near:  # smoothing/resampling can produce off-table colours
        _near[rgba] = min(LUT.items(), key=lambda kv: sum((a - b) ** 2 for a, b in zip(kv[0], rgba)))[1]
    return _near[rgba]


def merc_px(lon: float, lat: float) -> tuple[float, float]:
    s = math.sin(math.radians(lat))
    return (lon + 180) / 360 * WORLD, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * WORLD


class MercFrame:
    """One RainViewer time step: z7 tiles decoded to level bytes."""

    def __init__(self, t: int, tiles: dict[tuple[int, int], bytes | None]):
        self.t, self.tiles = t, tiles

    def level(self, px: int, py: int) -> int:
        b = self.tiles.get((px // TILE, py // TILE))
        return 0 if b is None else b[(py % TILE) * TILE + (px % TILE)]


class EquiFrame:
    """IEM n0q composite crop: plain lat/lon grid."""

    def __init__(self, t: int, lon0: float, lat0: float, res: float, w: int, h: int, data: bytes):
        self.t, self.lon0, self.lat0, self.res, self.w, self.h, self.data = t, lon0, lat0, res, w, h, data


def _decode_tile(raw: bytes) -> bytes | None:
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    if im.getchannel("A").getbbox() is None:
        return None  # empty tile
    arr = np.frombuffer(im.tobytes(), dtype="<u4")  # RGBA packed little-endian
    colours, inverse = np.unique(arr, return_inverse=True)
    lv = np.array([0 if (c >> 24) == 0 else
                   _lookup((int(c) & 255, (int(c) >> 8) & 255, (int(c) >> 16) & 255, int(c) >> 24))
                   for c in colours], dtype=np.uint8)
    return lv[inverse].tobytes()


def bbox_for(w: int, h: int, span_lon: float | None = None, lat: float | None = None, lon: float | None = None):
    """Map box for a w x h dot grid with square dots, centred on home."""
    span_lon = span_lon or config.MAP_SPAN_LON
    lat = config.LAT if lat is None else lat
    lon = config.LON if lon is None else lon
    span_lat = span_lon * math.cos(math.radians(lat)) * h / w
    return (lon - span_lon / 2, lon + span_lon / 2, lat - span_lat / 2, lat + span_lat / 2)


class Radar:
    def __init__(self):
        self.frames: dict[str, MercFrame | EquiFrame] = {}
        self.source = "none"
        self.lock = threading.Lock()
        self._cache: dict[tuple, dict] = {}

    # -- fetching --
    def _tile_range(self):
        # cover a square-ish box (aspect up to 1:1) around home, with margin
        lon0, lon1, lat0, lat1 = bbox_for(100, 100)
        m = 0.3
        x0, y0 = merc_px(lon0 - m, lat1 + m)
        x1, y1 = merc_px(lon1 + m, lat0 - m)
        return [(x, y) for x in range(int(x0) // TILE, int(x1) // TILE + 1)
                for y in range(int(y0) // TILE, int(y1) // TILE + 1)]

    def refresh(self) -> dict:
        try:
            return self._refresh_rainviewer()
        except Exception as e:  # noqa: BLE001
            # Keep showing the loop we have; IEM is a US-only single frame, so it's a fallback for NWS places only
            if any(isinstance(f, MercFrame) for f in self.frames.values()) or config.PROVIDER != "nws":
                raise
            info = self._refresh_iem()
            info["rainviewer_error"] = repr(e)
            return info

    # tiles are kept on disk so a restart doesn't refetch the whole loop (10 collectors at once trip a 429)
    TILE_DIR = Path(config.DATA_DIR) / "tiles"

    def _tile(self, host: str, key: str, xy: tuple[int, int]) -> bytes | None:
        f = self.TILE_DIR / f"{key.strip('/').replace('/', '_')}_{xy[0]}_{xy[1]}"
        if f.exists():
            b = f.read_bytes()
            return b or None
        dec = _decode_tile(get(f"{host}{key}/{TILE}/{Z}/{xy[0]}/{xy[1]}/2/0_0.png", timeout=20, tries=3))
        try:
            self.TILE_DIR.mkdir(parents=True, exist_ok=True)
            f.write_bytes(dec or b"")
        except OSError:
            pass
        return dec

    def _refresh_rainviewer(self) -> dict:
        maps = get_json("https://api.rainviewer.com/public/weather-maps.json")
        host, past = maps["host"], maps["radar"]["past"]
        if not past:
            raise RuntimeError("rainviewer: no frames")
        new, err = {}, None
        rng = self._tile_range()
        for fr in reversed(past):  # newest first, so a rate limit mid-way still leaves the latest frames
            key = fr["path"]
            if key in self.frames and isinstance(self.frames[key], MercFrame):
                new[key] = self.frames[key]
                continue
            try:
                with ThreadPoolExecutor(2) as ex:
                    tiles = list(ex.map(lambda xy, k=key: self._tile(host, k, xy), rng))
            except Exception as e:  # noqa: BLE001 - keep what we have, retry the rest on the next run
                err = e
                break
            new[key] = MercFrame(fr["time"], dict(zip(rng, tiles)))
        keep = {fr["path"].strip("/").replace("/", "_") for fr in past}
        for f in self.TILE_DIR.glob("*") if self.TILE_DIR.exists() else []:
            if f.name.rsplit("_", 2)[0] not in keep:
                f.unlink(missing_ok=True)
        if new:
            with self.lock:
                self.frames, self.source = new, "rainviewer"
                self._cache.clear()
        if err is not None:
            raise RuntimeError(f"rainviewer partial ({len(new)}/{len(past)} frames): {err!r}")
        return {"source": "rainviewer", "frames": len(new), "latest": past[-1]["time"]}

    def _refresh_iem(self) -> dict:
        base = "https://mesonet.agron.iastate.edu/data/gis/images/4326/USCOMP/n0q_0"
        wld = [float(x) for x in get(f"{base}.wld").split()]
        res, lon_tl, lat_tl = wld[0], wld[4], wld[5]
        im = Image.open(io.BytesIO(get(f"{base}.png", timeout=60)))
        lon0, lon1, lat0, lat1 = bbox_for(100, 100)
        m = 0.3
        c0, r0 = int((lon0 - m - lon_tl) / res), int((lat_tl - (lat1 + m)) / res)
        c1, r1 = int((lon1 + m - lon_tl) / res) + 1, int((lat_tl - (lat0 - m)) / res) + 1
        crop = im.crop((c0, r0, c1, r1))
        lut = bytes(level_for(i / 2 - 32, False) if i else 0 for i in range(256))  # n0q: dBZ = v/2 - 32
        data = crop.tobytes().translate(lut)
        fr = EquiFrame(int(time.time()) // 300 * 300, lon_tl + c0 * res, lat_tl - r0 * res, res, c1 - c0, r1 - r0, data)
        with self.lock:
            self.frames, self.source = {"iem": fr}, "iem"
            self._cache.clear()
        return {"source": "iem", "frames": 1}

    # -- sampling --
    def grid(self, w: int, h: int, bbox=None, last: int = 0) -> dict:
        """Level rows for every frame (oldest first), or only the newest `last` frames."""
        w, h = max(2, min(w, 800)), max(2, min(h, 600))
        bbox = tuple(bbox) if bbox else bbox_for(w, h)
        with self.lock:
            frames = sorted(self.frames.items(), key=lambda kv: kv[1].t)
            source = self.source
        if last > 0:
            frames = frames[-last:]
        out = []
        for key, fr in frames:
            ck = (key, w, h, bbox)
            if ck not in self._cache:
                self._cache[ck] = _sample(fr, w, h, bbox)
            out.append({"t": fr.t, "rows": self._cache[ck]})
        return {"source": source, "bbox": bbox, "w": w, "h": h, "frames": out}


def _ch(v: int) -> str:
    return str(v) if v < 6 else "abcdef"[min(v - 10, 5)]


def _sample(fr, w, h, bbox) -> list[str]:
    lon0, lon1, lat0, lat1 = bbox
    rows = []
    if isinstance(fr, MercFrame):
        xs = [merc_px(lon0 + (lon1 - lon0) * i / w, lat0)[0] for i in range(w + 1)]
        ys = [merc_px(lon0, lat1 - (lat1 - lat0) * j / h)[1] for j in range(h + 1)]
        for j in range(h):
            ya, yb = int(ys[j]), max(int(ys[j]) + 1, int(ys[j + 1]))
            row = []
            for i in range(w):
                xa, xb = int(xs[i]), max(int(xs[i]) + 1, int(xs[i + 1]))
                best = 0
                for py in range(ya, yb):
                    for px in range(xa, xb):
                        v = fr.level(px, py)
                        if v % 10 > best % 10:
                            best = v
                row.append(_ch(best))
            rows.append("".join(row))
    else:
        for j in range(h):
            la, lb = lat1 - (lat1 - lat0) * j / h, lat1 - (lat1 - lat0) * (j + 1) / h
            ra, rb = int((fr.lat0 - la) / fr.res), max(int((fr.lat0 - la) / fr.res) + 1, int((fr.lat0 - lb) / fr.res))
            row = []
            for i in range(w):
                oa, ob = lon0 + (lon1 - lon0) * i / w, lon0 + (lon1 - lon0) * (i + 1) / w
                ca = int((oa - fr.lon0) / fr.res)
                cb = max(ca + 1, int((ob - fr.lon0) / fr.res))
                best = 0
                for r in range(max(ra, 0), min(rb, fr.h)):
                    for c in range(max(ca, 0), min(cb, fr.w)):
                        best = max(best, fr.data[r * fr.w + c])
                row.append(_ch(best))
            rows.append("".join(row))
    return rows
