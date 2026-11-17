"""Render the screen headless at a given size and save an SVG per theme. Usage: python -m wxterm.snap OUTDIR [W H]"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="wxsnap-")  # never touch the real saved theme/location
os.environ.pop("WXTERM_STATE", None)

from .app import WxApp  # noqa: E402
from .panels import ExtrasPanel  # noqa: E402
from .themes import THEMES  # noqa: E402


async def run(out: str, w: int, h: int, themes: list[str]):
    app = WxApp()
    async with app.run_test(size=(w, h)) as pilot:
        for _ in range(60):  # wait for data + radar
            await pilot.pause(0.5)
            if app.data and app.radar and app.geo:
                break
        app.radar_paused = True
        app.radar_idx = len((app.radar or {}).get("frames") or [1]) - 1
        bad = []
        for k in range(len(ExtrasPanel.available(app))):  # every rotating extras slot, every panel: no ERROR boxes
            app.extras_idx = k
            await pilot.pause(0.3)
            for wdg in app.query("#now, #radar, #aprsmap, #graph, #tide, #days, #extras, #status"):
                txt = str(wdg.render())
                if "ERROR" in txt[:400]:
                    bad.append((k, wdg.id, txt[:300]))
        print("panel errors:", bad or "none")
        for i, th in enumerate(THEMES):
            if themes and th.name not in themes:
                continue
            app.theme_idx = i
            app.extras_idx = i % 3
            app.extras_since -= 20
            app._apply_theme()
            await pilot.pause(0.6)
            app.save_screenshot(f"{app.loc}-{th.name.lower()}.svg", out)
            print("saved", app.loc, th.name)


if __name__ == "__main__":
    out = sys.argv[1]
    w, h = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (240, 67)
    asyncio.run(run(out, w, h, [a.upper() for a in sys.argv[4:]]))
