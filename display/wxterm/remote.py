"""Hooks for remote readers (the MCP server): a status file and screenshot requests, both in the state directory.

- STATE_DIR/display-status.json: what the screen is doing, rewritten every 10 s.
- STATE_DIR/requests/<id>.shot: a screenshot request. The display answers with <id>.png (or <id>.err) in the same
  folder within a couple of seconds. Requests and answers older than a minute are swept away.
The display never reads anything else from that folder, so a client can't make it do more than take a picture.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def write_status(state_file: Path, status: dict) -> None:
    path = state_file.parent / "display-status.json"
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, separators=(",", ":")))
        tmp.replace(path)
    except OSError:
        pass


def pending_shots(state_file: Path) -> list[tuple[str, float]]:
    """(id, scale) for each new screenshot request; claims each one by deleting it. Sweeps stale files."""
    req = state_file.parent / "requests"
    out = []
    try:
        entries = list(req.iterdir())
    except OSError:
        return out
    now = time.time()
    for p in entries:
        try:
            if now - p.stat().st_mtime > 60:
                p.unlink(missing_ok=True)
                continue
        except OSError:
            continue
        if p.suffix != ".shot" or not ID.match(p.stem):
            continue
        try:
            scale = float(json.loads(p.read_text() or "{}").get("scale", 1))
        except (OSError, ValueError, AttributeError):
            scale = 1.0
        p.unlink(missing_ok=True)
        out.append((p.stem, min(1.0, max(0.25, scale))))
    return out


def take_shot(state_file: Path, shot_id: str, scale: float) -> None:
    """Capture the kiosk screen with grim into requests/<id>.png, or write the reason to <id>.err."""
    req = state_file.parent / "requests"
    out, err = req / f"{shot_id}.png", req / f"{shot_id}.err"
    try:
        if not os.environ.get("WAYLAND_DISPLAY") or not shutil.which("grim"):
            raise RuntimeError("screenshots need the kiosk screen (grim under cage)")
        tmp = req / f".{shot_id}.png"
        r = subprocess.run(["grim", "-t", "png", "-s", f"{scale:g}", str(tmp)], capture_output=True, text=True,
                           timeout=15)
        if r.returncode != 0 or not tmp.exists():
            raise RuntimeError(f"grim failed: {r.stderr.strip()[-120:]}")
        os.chmod(tmp, 0o664)
        tmp.replace(out)
    except Exception as e:  # noqa: BLE001 - report any failure back to the requester
        try:
            err.write_text(str(e)[:200])
        except OSError:
            pass
