"""wxterm's APRS parser (collector/wxcollect/aprsparse.py) on worked examples from the APRS spec (APRS101) and typical
packets. Units follow aprslib: km/h, metres, °C, m/s, mm, mb. (During development the parser was also checked
against aprslib on 20,000 randomly encoded packets; aprslib is GPL, so it isn't a test dependency.)"""
from __future__ import annotations

import pytest

from wxcollect.aprsparse import parse


def test_uncompressed_position_course_speed_altitude():
    p = parse("N0CALL>APRS,WIDE1-1,qAR,K6IGT:=4903.50N/07201.75W>088/036/A=001234 mobile")
    assert p["format"] == "uncompressed" and p["from"] == "N0CALL" and p["via"] == "K6IGT"
    assert p["latitude"] == pytest.approx(49.058333, abs=1e-6) and p["longitude"] == pytest.approx(-72.029167, abs=1e-6)
    assert (p["symbol_table"], p["symbol"], p["course"]) == ("/", ">", 88)
    assert p["speed"] == pytest.approx(36 * 1.852) and p["altitude"] == pytest.approx(1234 * 0.3048)
    assert p["comment"] == "mobile"


def test_timestamped_and_ambiguous_positions():
    assert parse("N0CALL>APRS:/092345z4903.50N/07201.75W>")["latitude"] == pytest.approx(49.058333, abs=1e-6)
    p = parse("N0CALL>APRS:!49  .  N/072  .  W-")        # ambiguous to the degree: the middle of the box
    assert p["latitude"] == pytest.approx(49.5) and p["longitude"] == pytest.approx(-72.5)


def test_compressed_spec_examples():
    p = parse("N0CALL>APRS:!/5L!!<*e7>7P[")              # APRS101 ch. 9: 49°30'N 72°45'W, 088°, 36.2 kn
    assert p["format"] == "compressed"
    assert p["latitude"] == pytest.approx(49.5, abs=1e-5) and p["longitude"] == pytest.approx(-72.75, abs=1e-5)
    assert p["course"] == 88 and p["speed"] == pytest.approx(36.2 * 1.852, abs=0.1)
    a = parse("N0CALL>APRS:!/5L!!<*e7OS]S")              # same place, altitude 10,004 ft from a GGA fix
    assert a["altitude"] == pytest.approx(10004 * 0.3048, abs=1) and "course" not in a


def test_mic_e_spec_destination():
    p = parse('N0CALL>S32U6T,WIDE1-1,qAR,IGATE:`(_fn"Oj/]')  # S32U6T: 33°25.64'N, west, no longitude offset
    assert p["format"] == "mic-e" and p["latitude"] == pytest.approx(33 + 25.64 / 60, abs=1e-6)
    assert p["longitude"] < 0 and (p["symbol_table"], p["symbol"]) == ("/", "j")


def test_weather_station_with_position():
    p = parse("FW1234>APRS:@171415z3748.00N/12227.00W_180/005g012t045r000p012P010h65b10132L000.WD 31")
    w = p["weather"]
    assert "course" not in p and "speed" not in p        # a weather station's ddd/sss is wind, not motion
    assert w["wind_direction"] == 180 and w["wind_speed"] == pytest.approx(5 * 0.44704)
    assert w["wind_gust"] == pytest.approx(12 * 0.44704) and w["temperature"] == pytest.approx((45 - 32) / 1.8)
    assert w["humidity"] == 65 and w["pressure"] == pytest.approx(1013.2) and w["rain_24h"] == pytest.approx(3.048)
    assert parse("X>APRS:=4903.50N/07201.75W_090/010t-05h00")["weather"]["humidity"] == 100   # h00 = 100%


def test_positionless_weather():
    p = parse("N0CALL>APRS:_10090556c220s004g005t077r000p000P000h50b09900wRSW")
    assert p["format"] == "wx" and p["weather"]["wind_direction"] == 220
    assert p["weather"]["wind_speed"] == pytest.approx(4 * 0.44704) and p["weather"]["pressure"] == pytest.approx(990)


def test_objects_items_status_messages():
    o = parse("N0CALL>APRS:;LEADER   *092345z4903.50N/07201.75W>088/036")
    assert (o["format"], o["object_name"], o["alive"]) == ("object", "LEADER", True)
    assert parse("N0CALL>APRS:;LEADER   _092345z4903.50N/07201.75W>")["alive"] is False
    i = parse("N0CALL>APRS:)AID #2!4903.50N/07201.75WA")
    assert (i["format"], i["item_name"], i["alive"]) == ("item", "AID #2", True)
    assert parse("N0CALL>APRS:>Net Control")["status"] == "Net Control"
    assert parse("N0CALL>APRS::N0CALL-1 :hello{001")["format"] == "message"
    assert parse("N0CALL>APRS::BLN1     :net tonight")["format"] == "bulletin"


@pytest.mark.parametrize("line", ["garbage", "N0CALL>APRS:", "N0CALL>APRS:T#005,199,000,255,073,123,01101001",
                                  "N0CALL>APRS:!9903.50N/07201.75W-", "N0CALL>APRS:}third-party", b"\xff\xfe"])
def test_what_we_skip(line):
    with pytest.raises(ValueError):
        parse(line)
