from __future__ import annotations

import json
import time
import urllib.request

from . import config


def get(url: str, timeout: int = 25, tries: int = 2, accept: str | None = None) -> bytes:
    headers = {"User-Agent": config.UA}
    if accept:
        headers["Accept"] = accept
    last: Exception | None = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - retried, then re-raised
            last = e
            if i + 1 < tries:
                if getattr(e, "code", None) == 429:  # rate limited: honour Retry-After, else back off hard
                    ra = (getattr(e, "headers", None) or {}).get("Retry-After")
                    time.sleep(min(60, int(ra)) if ra and str(ra).isdigit() else 10 * (i + 1))
                else:
                    time.sleep(3 * (i + 1))
    raise last  # type: ignore[misc]


def get_json(url: str, **kw) -> dict:
    return json.loads(get(url, **kw))
