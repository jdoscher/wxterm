"""scripts/wxctl.sh map and the kiosk's MAP setting (kiosk/lib.sh), run in a temp state dir."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")


def wxctl(tmp_path, *args):
    env = os.environ | {"WXTERM_STATE": str(tmp_path / "state.json"), "STATE_DIR": str(tmp_path)}
    return subprocess.run(["bash", str(ROOT / "scripts" / "wxctl.sh"), *args], capture_output=True, text=True,
                          env=env)


def test_map_command_sets_the_map_and_keeps_other_settings(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"theme": "GREEN", "location": "sf", "map": "radar"}))
    r = wxctl(tmp_path, "map", "AIR")
    assert r.returncode == 0
    assert json.loads((tmp_path / "state.json").read_text()) == {"theme": "GREEN", "location": "sf", "map": "air"}
    wxctl(tmp_path, "map", "radar")
    assert json.loads((tmp_path / "state.json").read_text())["map"] == "radar"


def test_map_command_rejects_anything_else(tmp_path):
    r = wxctl(tmp_path, "map", "planes")
    assert r.returncode != 0 and "air or radar" in r.stderr
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("given, expect", [("", "radar"), ("air", "air")])
def test_kiosk_map_setting_defaults_to_radar(given, expect):
    env = {k: v for k, v in os.environ.items() if k != "MAP"} | ({"MAP": given} if given else {})
    r = subprocess.run(["bash", "-c", f'. "{ROOT}/kiosk/lib.sh"; echo "$MAP"'], capture_output=True, text=True,
                       env=env)
    assert r.stdout.strip() == expect
