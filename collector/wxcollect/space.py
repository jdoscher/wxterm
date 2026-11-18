"""Orbital elements for the space layer, from CelesTrak (free, no key).

CelesTrak updates each group about every 2 hours and refuses (HTTP 403, or a plain-text note) to send the same
group to the same address again before it changes. So each group is fetched at most once every 6 hours, kept on
disk, and served from the cache in between. A refusal keeps the cached copy. The display asks one collector (the
first location) for every group, so a house full of collectors still makes one request per group.

GET /api/tle?group=stations|visual|starlink -> {"group", "fetched", "checked", "error", "records": [OMM, ...]}
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from . import config
from .net import get

log = logging.getLogger("wxcollect.space")
URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={}&FORMAT=json"
GROUPS = ("stations", "visual", "starlink")
MAX_AGE = 6 * 3600        # fetch again after this long
RETRY = 2 * 3600          # after a failure or refusal, don't ask again for this long
KEEP = ("OBJECT_NAME", "OBJECT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE",
        "ARG_OF_PERICENTER", "MEAN_ANOMALY", "EPHEMERIS_TYPE", "CLASSIFICATION_TYPE", "NORAD_CAT_ID", "ELEMENT_SET_NO",
        "REV_AT_EPOCH", "BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT")

_locks = {g: threading.Lock() for g in GROUPS}
_mem: dict[str, dict] = {}


def _path(group: str) -> Path:
    return Path(config.DATA_DIR) / f"tle-{group}.json"


def _load(group: str) -> dict | None:
    if group in _mem:
        return _mem[group]
    try:
        _mem[group] = json.loads(_path(group).read_text())
        return _mem[group]
    except (OSError, ValueError):
        return None


def _save(group: str, ent: dict):
    _mem[group] = ent
    try:
        p = _path(group)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(ent))
        tmp.replace(p)
    except OSError as e:
        log.warning("tle cache save failed: %s", e)


def elements(group: str, now: float | None = None) -> dict:
    """The group's elements, fetched if the cache is old (and we're allowed to ask). Raises ValueError for an
    unknown group, LookupError if there's nothing to serve at all."""
    if group not in GROUPS:
        raise ValueError(f"unknown group {group}")
    now = now or time.time()
    with _locks[group]:
        ent = _load(group)
        due = not ent or (now - ent.get("fetched", 0) > MAX_AGE and now - ent.get("checked", 0) > RETRY)
        if due:
            err = None
            try:
                body = get(URL.format(group), timeout=60, tries=1, accept="application/json")
                recs = json.loads(body)
                if not isinstance(recs, list) or not recs:
                    raise ValueError("no elements in the answer")
                ent = {"group": group, "fetched": now, "checked": now, "error": None,
                       "records": [{k: r[k] for k in KEEP if k in r} for r in recs]}
                log.info("tle %s: %d objects", group, len(recs))
            except Exception as e:  # noqa: BLE001 - 403/plain text = "not changed since your last download"
                code = getattr(e, "code", None)
                err = f"HTTP {code}" if code else type(e).__name__
                log.warning("tle %s not fetched (%s); keeping the cached copy", group, err)
                ent = (ent or {"group": group, "fetched": 0, "records": []}) | {"checked": now, "error": err}
            _save(group, ent)
        if not ent or not ent.get("records"):
            raise LookupError((ent or {}).get("error") or "no elements yet")
        return ent
