"""Record the live screen to an animated GIF (key `g`).

Runs inside the kiosk's Wayland session (cage): wf-recorder captures the output to a temporary MP4, then ffmpeg
turns it into a palette-optimised GIF. Recording length is one full radar loop, so the GIF loops cleanly.
Output dir: WXTERM_GIF_DIR (the kiosk sets STATE_DIR/gifs, linked as ~/gifs for ADMIN_USERS), default ~/gifs.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

_KIOSK = Path("/var/lib/wxterm")
GIF_DIR = Path(os.environ.get("WXTERM_GIF_DIR") or
               (_KIOSK / "gifs" if os.access(_KIOSK, os.W_OK) else Path.home() / "gifs"))
FPS = int(os.environ.get("WXTERM_GIF_FPS", "10"))


def available() -> str | None:
    """None if recording can work here, else the reason it can't."""
    if not os.environ.get("WAYLAND_DISPLAY"):
        return "GIF NEEDS THE KIOSK SCREEN"
    for tool in ("wf-recorder", "ffmpeg"):
        if not shutil.which(tool):
            return f"GIF NEEDS {tool.upper()}"
    return None


def record(seconds: float, name: str, status=lambda msg: None) -> Path:
    """Capture `seconds` of screen, encode to GIF_DIR/<name>-<timestamp>.gif and return the path."""
    GIF_DIR.mkdir(parents=True, exist_ok=True)
    out = GIF_DIR / f"wxterm-{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.gif"
    with tempfile.TemporaryDirectory(prefix="wxgif-") as tmp:
        mp4 = Path(tmp) / "cap.mp4"
        rec = subprocess.Popen(["wf-recorder", "-y", "-f", str(mp4)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(seconds)
        rec.send_signal(signal.SIGINT)  # wf-recorder finalises the file on SIGINT
        try:
            rec.wait(timeout=15)
        except subprocess.TimeoutExpired:
            rec.kill()
        if not mp4.exists() or mp4.stat().st_size == 0:
            raise RuntimeError("wf-recorder produced no video")
        status("ENCODING GIF…")
        vf = (f"fps={FPS},split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
              "[b][p]paletteuse=dither=none:diff_mode=rectangle")
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-vf", vf, "-loop", "0",
                            str(out)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-120:]}")
    os.chmod(out, 0o664)
    return out
