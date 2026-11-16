# Changelog

## 1.0.0 (2026-11-16)

First public release.

**Display**
- Full-screen terminal weather display in Python and Textual: a radar loop over a line-drawn map, current conditions
  with seven-segment digits, a 48-hour braille graph, tides and moon, a 5-day forecast, and a rotating panel (NWS
  forecast discussion, surf, coast-to-inland temperatures).
- Five themes (amber, green, white, IBM blue, colour), three big-digit styles, and ten built-in locations, all
  switchable from the keyboard.
- One-key GIF capture of exactly one radar loop.
- Burn-in guard: the layout shuffles every 15 minutes.
- Works at any terminal size from about 160x45; designed for 240x62 at 1920x1080.

**Data**
- One Docker collector per location, listening on localhost only. Free sources, no API keys: NWS, aviationweather.gov
  METARs, Open-Meteo forecast and marine, RainViewer radar (IEM NEXRAD fallback), NOAA CO-OPS tides; sun and moon
  computed locally.
- `scripts/new-location.py NAME LAT LON` sets up any place on Earth: stations, tides, surf, map lines and labels.

**Kiosk**
- `kiosk/setup.sh` turns a Debian or Ubuntu machine into a dedicated weather terminal (cage + foot on tty1, no
  desktop). Every setting lives in `kiosk/kiosk.conf`; `mode.sh` switches between kiosk and desktop.

**MCP**
- Optional read-only MCP server (off by default): weather, radar summary, tides, alerts, kiosk status with machine
  stats, and screenshots. Bearer token, LAN only, browser requests refused.
