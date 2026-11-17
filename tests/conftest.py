"""Shared setup. Runs before any test module imports wxterm or wxcollect, so both read safe settings:
the display's saved state goes to a temp dir (never ~/.config/wxterm), and the collector loads a demo map and
writes its cache to a temp dir. No test touches the network: collector calls go through fakes."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="wxterm-tests-"))

os.environ["WXTERM_STATE"] = str(_TMP / "state" / "state.json")
os.environ["WXTERM_GIF_DIR"] = str(_TMP / "gifs")
for k in ("WXTERM_AIR", "WXTERM_LOCATION", "WXTERM_LOCATIONS", "WXTERM_ONLY", "WXTERM_API", "WXTERM_THEME"):
    os.environ.pop(k, None)
os.environ["WXTERM_SHUFFLE_MIN"] = "0"

os.environ["WX_GEO"] = str(ROOT / "locations" / "sf.geo.json")
os.environ["WX_DATA_DIR"] = str(_TMP / "collector")
os.environ["WX_LAT"], os.environ["WX_LON"] = "37.7749", "-122.4194"
