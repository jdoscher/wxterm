# Changelog

## 1.4.0 (2026-11-18)

- **APRS**, contributed by [Steve Miller, KC1AWV](https://github.com/kc1awv) (thanks, Steve): with a callsign in
  `APRS_CALL`, the home collector listens to APRS-IS around home (receive-only, passcode -1) and the map draws what it
  hears as a fourth layer (`x`: all stations, weather stations only, off), with heard-list and weather-station panels.
- APRS packets are read by wxterm's own parser (MIT), checked against the spec's worked examples.
- `APRS_CALL`, `APRS_LOCATIONS`, `APRS_RANGE_KM` and `APRS_VIEW` in `kiosk.conf`; `scripts/wxctl.sh aprs off|wx|all`.

## 1.3.0 (2026-11-17)

- **Spacecraft layer** (`o`, on by default, `SPACE` in `kiosk.conf`): the space stations, the brightest satellites and
  Starlink cross the map with their names, altitudes and ground tracks. The top border counts down to the next station
  overhead, and the rotating panel lists the stations' visible passes. Orbits from CelesTrak, fetched by one collector
  at most every 6 hours; positions computed locally with SGP4.
- **Suspend on lid close** (`LID=suspend` in `kiosk.conf`): the laptop sleeps when the lid closes and wakes when it
  opens. On waking the display drops old aircraft and radar and refreshes everything, and the collectors catch up.
- `scripts/wxctl.sh space on|off`. The MCP server's `get_status` reports the space layer.

## 1.2.0 (2026-11-17)

- **Air traffic**, contributed by [Justin Van Bibber](https://github.com/notjvb) (thanks, Justin): live ADS-B aircraft from adsb.fi (adsb.lol as a fallback) with
  trails, data tags, routes from adsbdb, runways and airport codes from OurAirports, a traffic list and the selected
  flight's details.
- **One map, two layers.** The radar loop (`w`) and air traffic (`a`) switch on and off on their own, over one map
  centred on home. `+` and `-` change the range (5 to 100 NM, 40 to start) for both, and the radar is sampled for
  the range on screen, so zooming in shows finer rain.
- `AIR` in `kiosk.conf` (off by default), and `scripts/wxctl.sh air on|off`, `radar on|off`, `range NM` and
  `tags auto|all|off`.
- Map labels keep a space between them, so neighbouring airport codes no longer run together.
- The MCP server's `get_status` reports the map's layers and range.
- Tests: `pip install -r requirements-dev.txt`, then `pytest`.

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
