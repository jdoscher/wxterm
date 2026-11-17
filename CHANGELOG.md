# Changelog

## Unreleased

- **APRS**, optional and receive-only: set `APRS_CALL` and the home collector listens to APRS-IS around home. `x` draws
  the stations over the radar loop (all of them, or only weather stations' temperatures), or turns the big map into an
  APRS map with tracks, range rings (`[` `]`), the latest radar frame and a heard list. Two new rotating panels: APRS
  HEARD and APRS WEATHER. `wxctl.sh aprs off|wx|all|map`, `APRS_VIEW` first-run setting, APRS state in the MCP
  `get_status` tool.
- Collector: `/api/aprs`, and `/api/radar` takes `&last=N` to return only the newest frames.
- Tests: `pytest` from the repo root (`requirements-dev.txt`).

## 1.1.0 (2026-11-16)

- Raspberry Pi support, **early testing**: `PROFILE=pi` in `kiosk.conf` (slower animation, four locations by default), and
  `LOCATIONS` to choose which collectors run on any machine. Setup skips groups a distro doesn't have.
  Tested under ARM64 emulation (images, collector, display, packages); reports from real Pis welcome.
- The display uses about a quarter of the CPU it did: radar frames are drawn once per loop and cached, panels hand
  Textual ready-made lines instead of Rich text, and the rotating panel only redraws while it's typing.
- `RADAR_STEP` and `TYPE_STEP` settings for animation speed.

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
