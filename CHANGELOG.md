# Changelog

## Unreleased

**Air traffic map**
- `a` switches the big map between the radar loop and live air traffic, ported from AsciiFlights: ADS-B aircraft as
  arrows with braille trails, dead-reckoned between polls, over the location's map with runways, airport codes and
  range rings, and the latest radar frame dithered underneath (`w`).
- Data tags with callsign, type, route or registration, altitude, climb/descent and speed, placed on leader lines so
  they don't overlap; `t` cycles auto, all and off. `+`/`-` change the range (5 to 100 NM), `Tab` selects the next
  aircraft, a click selects one, and `Esc` clears it.
- When the panel is wide enough, a traffic list and the selected aircraft's details (operator, route, vertical rate,
  distance and bearing from home, altitude profile) sit beside the map.
- Routes that don't fit the aircraft's position or heading are hidden, because route databases go stale.
- The choice of map, range, tag mode and radar underlay is saved. `MAP=air` in `kiosk.conf` makes it the first-run
  map, and `scripts/wxctl.sh map air|radar` switches it remotely. The MCP server's `get_status` reports it.

**Collector**
- `/api/aircraft?dist=NM` (adsb.fi, falling back to adsb.lol; at most one request a second, cached 1.5 s, fetched
  only while the air traffic map is on screen) and `/api/routes?cs=` (adsbdb, cached 6 hours).
- `/api/radar` takes `&last=N` to return only the newest frames.

**Locations**
- Geo files include large and medium airports and their runways from OurAirports (public domain). The demo cities are
  updated. For an older `home`, run `python3 scripts/make_geo.py --add-airports locations/home.geo.json`.

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
