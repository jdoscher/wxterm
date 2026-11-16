"""Collector client and the location list: plain urllib, no extra deps.

Locations come from WXTERM_LOCATIONS ("name=url,name=url,..."), else from locations/order in this checkout (only names
whose locations/<name>.env exists, collectors on localhost). WXTERM_API, if set, pins a single collector (for tests).
"""
from __future__ import annotations

import gzip
import json
import os
import urllib.request
from pathlib import Path

LOC_DIR = Path(os.environ.get("WXTERM_LOCATIONS_DIR") or Path(__file__).resolve().parents[2] / "locations")


def _from_order() -> list[tuple[str, str]]:
    out = []
    try:
        lines = (LOC_DIR / "order").read_text().splitlines()
    except OSError:
        return [("nyc", "http://localhost:8092")]
    for line in lines:
        f = line.split("#", 1)[0].split()
        if len(f) == 2 and (LOC_DIR / f"{f[0]}.env").exists():
            out.append((f[0], f"http://localhost:{f[1]}"))
    return out or [("nyc", "http://localhost:8092")]


def locations() -> list[tuple[str, str]]:
    if os.environ.get("WXTERM_API"):
        return [("api", os.environ["WXTERM_API"].rstrip("/"))]
    if not os.environ.get("WXTERM_LOCATIONS"):
        return _from_order()
    out = []
    for item in os.environ["WXTERM_LOCATIONS"].split(","):
        name, _, url = item.partition("=")
        if name.strip() and url.strip():
            out.append((name.strip().lower(), url.strip().rstrip("/")))
    return out


def get(base: str, path: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(base + path, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)
