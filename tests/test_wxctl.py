"""scripts/wxctl.sh air/radar and the kiosk's AIR setting (kiosk/lib.sh), run in a temp state dir."""
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


def test_air_and_radar_commands_set_the_layers_and_keep_other_settings(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"theme": "GREEN", "location": "sf"}))
    assert wxctl(tmp_path, "air", "ON").returncode == 0
    assert wxctl(tmp_path, "radar", "off").returncode == 0
    assert json.loads((tmp_path / "state.json").read_text()) == {"theme": "GREEN", "location": "sf", "air": True,
                                                                 "radar": False}
    wxctl(tmp_path, "air", "off")
    assert json.loads((tmp_path / "state.json").read_text())["air"] is False


@pytest.mark.parametrize("cmd", ["air", "radar"])
def test_layer_commands_reject_anything_else(tmp_path, cmd):
    r = wxctl(tmp_path, cmd, "maybe")
    assert r.returncode != 0 and "on or off" in r.stderr
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("given, expect", [("", "off"), ("on", "on")])
def test_kiosk_air_setting_defaults_to_off(tmp_path, given, expect):
    (tmp_path / "kiosk").mkdir()   # a copy of lib.sh without this checkout's own kiosk.conf next to it
    shutil.copy(ROOT / "kiosk" / "lib.sh", tmp_path / "kiosk" / "lib.sh")
    env = {k: v for k, v in os.environ.items() if k != "AIR"} | ({"AIR": given} if given else {})
    r = subprocess.run(["bash", "-c", f'. "{tmp_path}/kiosk/lib.sh"; echo "$AIR"'], capture_output=True, text=True,
                       env=env)
    assert r.stdout.strip() == expect


def test_range_and_tags_commands(tmp_path):
    assert wxctl(tmp_path, "range", "25").returncode == 0
    assert wxctl(tmp_path, "tags", "all").returncode == 0
    assert json.loads((tmp_path / "state.json").read_text()) == {"air_range": 25, "air_tags": "ALL"}
    assert wxctl(tmp_path, "range", "30").returncode != 0
    assert wxctl(tmp_path, "tags", "some").returncode != 0
