from wth_edge import telemetry


def test_parse_throttled_flags():
    flags = telemetry.parse_throttled("throttled=0x50005")
    assert flags["raw"] == "0x50005"
    assert flags["under_voltage_now"] and flags["throttled_now"]
    assert flags["under_voltage_occurred"] and flags["throttled_occurred"]
    assert not flags["freq_capped_now"]


def test_parse_throttled_clean_and_missing():
    assert telemetry.parse_throttled("throttled=0x0")["under_voltage_now"] is False
    assert telemetry.parse_throttled(None) is None
    assert telemetry.parse_throttled("garbage") is None


def test_parse_pmic_ext5v():
    assert telemetry.parse_pmic_ext5v("EXT5V_V volt(24)=5.12765000V") == 5.128
    assert telemetry.parse_pmic_ext5v("") is None


def test_snapshot_runs_off_a_pi():
    snap = telemetry.snapshot()
    for key in ("cpu_percent", "ram_percent", "disk_percent", "cpu_temp_c", "input_voltage_v", "throttled"):
        assert key in snap
