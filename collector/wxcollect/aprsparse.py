"""A small APRS packet parser: the parts wxterm draws (positions, objects, items, Mic-E, weather, status).

MIT-licensed, written for wxterm so the project doesn't depend on a GPL library. parse() returns a dict with the same
keys and units aprslib uses for these packet types, so aprs.ingest() reads either:

  from, to, path, via, format ("uncompressed", "compressed", "mic-e", "object", "item", "status", "message", "wx",
  "telemetry-message", "bulletin"), latitude, longitude, symbol_table, symbol, comment, course (degrees),
  speed (km/h), altitude (m), object_name / item_name and alive, status, weather {temperature °C, wind_direction,
  wind_speed and wind_gust m/s, humidity %, pressure mb, rain_1h / rain_24h / rain_since_midnight mm}

Anything else (telemetry, NMEA, third-party, user-defined, malformed) raises ValueError; the caller skips it.
Reference: APRS Protocol Reference 1.0.1 (APRS101.PDF), chapters 6-12 and 17.
"""
from __future__ import annotations

import re

_HEADER = re.compile(r"^([A-Za-z0-9-]{1,9})>([A-Za-z0-9-]{1,9})((?:,[A-Za-z0-9*-]{1,9})*):(.*)$", re.S)
_Q = re.compile(r"^qA[A-Za-z]$")
_UNCOMP = re.compile(r"^(\d{2})([0-9 ]{2}\.[0-9 ]{2})([NnSs])(.)(\d{3})([0-9 ]{2}\.[0-9 ]{2})([EeWw])(.)")
_CSE_SPD = re.compile(r"^(\d{3}|\.{3}| {3})/(\d{3}|\.{3}| {3})")
_ALT = re.compile(r"/A=(-?\d{5,6})")
_WX_LEN = {"c": 3, "s": 3, "g": 3, "t": 3, "r": 3, "p": 3, "P": 3, "h": 2, "b": 5, "L": 3, "l": 3, "#": 3}


def _b91(s: str) -> int:
    v = 0
    for ch in s:
        o = ord(ch) - 33
        if not 0 <= o < 91:
            raise ValueError("bad base91")
        v = v * 91 + o
    return v


def _num(s: str) -> int | None:
    s = s.strip()
    return int(s) if s.lstrip("-").isdigit() else None


def weather(text: str, positionless: bool = False) -> tuple[dict, str]:
    """Weather fields at the start of `text` -> (aprslib-style weather dict, the rest of the text)."""
    w: dict = {}
    i = 0
    while i < len(text):
        code = text[i]
        n = _WX_LEN.get(code)
        if n is None or (code == "c" and not positionless):
            break
        raw = text[i + 1:i + 1 + n]
        if len(raw) < n or not re.fullmatch(r"-?\d+|\.+| +", raw):
            break
        v = _num(raw)
        i += 1 + n
        if v is None:
            continue
        if code == "c":
            w["wind_direction"] = v
        elif code == "s":   # wind speed in a positionless report; snowfall (not drawn) after a position
            if positionless:
                w["wind_speed"] = v * 0.44704
        elif code == "g":
            w["wind_gust"] = v * 0.44704
        elif code == "t":
            w["temperature"] = (v - 32) / 1.8
        elif code == "r":
            w["rain_1h"] = v * 0.254
        elif code == "p":
            w["rain_24h"] = v * 0.254
        elif code == "P":
            w["rain_since_midnight"] = v * 0.254
        elif code == "h":
            w["humidity"] = 100 if v == 0 else v
        elif code == "b":
            w["pressure"] = v / 10
        elif code in "Ll":
            w["luminosity"] = v + (1000 if code == "l" else 0)
    return w, text[i:]


def _comment(out: dict, rest: str, sym: str):
    """Data extension, altitude, weather and the comment that follow a position."""
    m = _CSE_SPD.match(rest)
    if m and sym != "_":
        c, s = _num(m.group(1)), _num(m.group(2))
        if c is not None and 1 <= c <= 360:
            out["course"] = c          # 360 = north; 0 = unknown
        if s is not None:
            out["speed"] = s * 1.852
        rest = rest[7:]
    elif m and sym == "_":   # a weather station: ddd/sss is wind direction and speed (mph), then the other fields
        w = {}
        c, s = _num(m.group(1)), _num(m.group(2))
        if c is not None:
            w["wind_direction"] = c
        if s is not None:
            w["wind_speed"] = s * 0.44704
        more, rest = weather(rest[7:])
        out["weather"] = w | more
    elif rest[:3] in ("PHG", "RNG", "DFS") and len(rest) >= 7:
        rest = rest[7:]
    a = _ALT.search(rest)
    if a:
        out["altitude"] = int(a.group(1)) * 0.3048
        rest = rest[:a.start()] + rest[a.end():]
    out["comment"] = rest.strip()


def _position(body: str, out: dict) -> None:
    """Uncompressed or compressed position at the start of body, and what follows it."""
    m = _UNCOMP.match(body)
    if m:
        # position ambiguity: spaces blank the last digits of the minutes; use the middle of the box they leave
        amb = m.group(2).count(" ")
        half = (0, 0.05, 0.5, 5, 30)[min(amb, 4)]
        lat = int(m.group(1)) + (float(m.group(2).replace(" ", "0")) + half) / 60
        lon = int(m.group(5)) + (float(m.group(6).replace(" ", "0")) + (half if amb else 0)) / 60
        if m.group(3) in "Ss":
            lat = -lat
        if m.group(7) in "Ww":
            lon = -lon
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("position out of range")
        out.update(format="uncompressed", latitude=lat, longitude=lon, symbol_table=m.group(4), symbol=m.group(8))
        _comment(out, body[19:], m.group(8))
        return
    if len(body) >= 13 and (body[0] in "/\\" or body[0].isalpha()):
        table = body[0]
        lat = 90 - _b91(body[1:5]) / 380926
        lon = -180 + _b91(body[5:9]) / 190463
        sym = body[9]
        out.update(format="compressed", latitude=lat, longitude=lon, symbol_table=table, symbol=sym)
        c, s, t = body[10], body[11], body[12]
        if c != " ":
            ctype = (ord(t) - 33) if "!" <= t <= "|" else 0
            if (ctype >> 3) & 3 == 2:          # source was a GGA sentence: cs is altitude
                out["altitude"] = 1.002 ** _b91(c + s) * 0.3048
            elif "!" <= c <= "z":                # course and speed
                out["course"] = (ord(c) - 33) * 4 or 360
                out["speed"] = (1.08 ** (ord(s) - 33) - 1) * 1.852
        rest = body[13:]
        if sym == "_":
            w, rest = weather(rest)
            if w:
                out["weather"] = w
        a = _ALT.search(rest)
        if a:
            out["altitude"] = int(a.group(1)) * 0.3048
            rest = rest[:a.start()] + rest[a.end():]
        out["comment"] = rest.strip()
        return
    raise ValueError("no position")


def _mic_e(dest: str, info: str, out: dict) -> None:
    """Mic-E: latitude, N/S, W/E and the longitude offset are in the destination call; the rest in the info field."""
    d = dest.split("-")[0]
    if len(d) != 6 or len(info) < 9:
        raise ValueError("short mic-e")
    digits = []
    for ch in d:
        if ch.isdigit():
            digits.append(ch)
        elif "A" <= ch <= "J":
            digits.append(str(ord(ch) - 65))
        elif "P" <= ch <= "Y":
            digits.append(str(ord(ch) - 80))
        elif ch in "KLZ":
            digits.append("0")   # position ambiguity
        else:
            raise ValueError("bad mic-e destination")
    lat = int("".join(digits[:2])) + float("".join(digits[2:4]) + "." + "".join(digits[4:6])) / 60
    north = "P" <= d[3] <= "Z"
    offset = 100 if "P" <= d[4] <= "Z" else 0
    west = "P" <= d[5] <= "Z"
    b = [ord(ch) - 28 for ch in info[1:7]]
    deg = b[0] + offset
    if 180 <= deg <= 189:
        deg -= 80
    elif 190 <= deg <= 199:
        deg -= 190
    mins = b[1] - 60 if b[1] >= 60 else b[1]
    lon = deg + (mins + b[2] / 100) / 60
    if not 0 <= lon <= 180 or not 0 <= lat <= 90:
        raise ValueError("mic-e position out of range")
    speed = b[3] * 10 + b[4] // 10
    if speed >= 800:
        speed -= 800
    course = (b[4] % 10) * 100 + b[5]
    if course >= 400:
        course -= 400
    out.update(format="mic-e", latitude=lat if north else -lat, longitude=-lon if west else lon,
               symbol=info[7], symbol_table=info[8], speed=speed * 1.852)
    if 0 < course <= 360:
        out["course"] = course
    rest = info[9:]
    if rest[:1] in (">", "]", "`", "'"):
        rest = rest[1:]
    if len(rest) >= 4 and rest[3] == "}":
        try:
            out["altitude"] = _b91(rest[:3]) - 10000
            rest = rest[4:]
        except ValueError:
            pass
    out["comment"] = rest.strip()


def parse(line: str | bytes) -> dict:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    m = _HEADER.match(line.rstrip("\r\n"))
    if not m:
        raise ValueError("not a TNC2 packet")
    src, dest, path, info = m.group(1), m.group(2), [p for p in m.group(3).split(",") if p], m.group(4)
    out: dict = {"raw": line, "from": src, "to": dest, "path": path}
    for i, p in enumerate(path):   # the igate that put it on the internet comes after the q construct
        if _Q.match(p) and i + 1 < len(path):
            out["via"] = path[i + 1]
    if not info:
        raise ValueError("empty")
    t = info[0]
    if t in "!=":
        _position(info[1:], out)
    elif t in "/@":
        _position(info[8:], out)
    elif t == ";":
        if len(info) < 18 or info[10] not in "*_":
            raise ValueError("bad object")
        out["object_name"] = info[1:10].strip()
        out["alive"] = info[10] == "*"
        _position(info[18:], out)
        out["format"] = "object"
    elif t == ")":
        m2 = re.match(r"\)([^!_]{3,9})([!_])", info)
        if not m2:
            raise ValueError("bad item")
        out["item_name"] = m2.group(1).strip()
        out["alive"] = m2.group(2) == "!"
        _position(info[m2.end():], out)
        out["format"] = "item"
    elif t in "`'\x1c\x1d":
        _mic_e(dest, info, out)
    elif t == "_":
        if len(info) < 9:
            raise ValueError("short weather")
        w, rest = weather(info[9:], positionless=True)
        if not w:
            raise ValueError("no weather")
        out.update(format="wx", weather=w, comment=rest.strip())
    elif t == ">":
        out.update(format="status", status=info[1:].strip())
    elif t == ":":
        addressee = info[1:10].strip()
        text = info[11:]
        if addressee.startswith("BLN"):
            out["format"] = "bulletin"
        elif re.match(r"(PARM|UNIT|EQNS|BITS)\.", text):
            out["format"] = "telemetry-message"
        else:
            out["format"] = "message"
        out.update(addresse=addressee, message_text=text)
    else:
        raise ValueError(f"unsupported packet type {t!r}")
    return out
