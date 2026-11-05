from wth_edge.weather import classify, parse


def test_wmo_codes_map_to_kinds():
    assert classify(0) == "clear" and classify(3) == "cloudy" and classify(45) == "fog"
    assert classify(61) == "rain" and classify(65) == "heavy_rain" and classify(73) == "snow"
    assert classify(95) == "thunderstorm" and classify(None) == "unknown"


def test_parse_flags_low_visibility():
    body = {"current": {"weather_code": 45, "precipitation": 0.0, "visibility": 400, "temperature_2m": 6.5,
                        "wind_speed_10m": 3.0, "is_day": 1}}
    s = parse(body, 1000.0, at=1.0)
    assert s["ok"] and s["kind"] == "fog" and s["low_visibility"] and s["visibility_m"] == 400 and s["at"] == 1.0
    clear = parse({"current": {"weather_code": 0, "visibility": 24000}}, 1000.0)
    assert clear["kind"] == "clear" and not clear["low_visibility"]
    assert not parse({}, 1000.0)["low_visibility"]
