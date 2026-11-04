from pathlib import Path

import pytest

from wth_edge import config as cfgmod

REPO = Path(__file__).resolve().parent.parent


def write(tmp_path, text):
    p = tmp_path / "device.toml"
    p.write_text(text)
    return p


def test_every_profile_loads(tmp_path):
    for name in cfgmod.available("profiles"):
        cfg = cfgmod.load(write(tmp_path, f'[device]\nid = "t"\nprofile = "{name}"\n'))
        assert cfg.device.profile == name
        assert cfg.camera.driver in {"picamera2", "fake", "file"}
        assert cfg.display.driver in {"gc9a01", "png", "none"}


def test_pi5_profile_matches_verified_wiring(tmp_path):
    cfg = cfgmod.load(write(tmp_path, '[device]\nid = "jf-pi-5"\nprofile = "pi5-cam3-gc9a01"\n'))
    assert (cfg.display.dc_pin, cfg.display.rst_pin, cfg.display.spi_device) == (25, 27, 0)
    assert cfg.hardware.model_match == "Raspberry Pi 5"
    assert cfg.paths.state_file == "/run/wth/state.json"
    assert set(cfg.site.rois) == {"boat", "bridge"}


def test_device_file_overrides_profile(tmp_path):
    cfg = cfgmod.load(write(tmp_path, '[device]\nprofile = "pi5-cam3-gc9a01"\n[camera]\nframerate = 10\n'))
    assert cfg.camera.framerate == 10.0
    assert cfg.camera.lores_format == "RGB888"  # untouched keys come from the profile


def test_relative_paths_resolve_next_to_device_file(tmp_path):
    cfg = cfgmod.load(write(tmp_path, '[device]\nprofile = "dev"\n'))
    assert cfg.paths.state_file == str((tmp_path / "run/state.json").resolve())


def test_unknown_profile_is_a_clear_error(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="no profile named 'nope'"):
        cfgmod.load(write(tmp_path, '[device]\nprofile = "nope"\n'))


def test_lists_become_tuples_and_unknown_keys_are_ignored():
    cfg = cfgmod.from_dict({"camera": {"main_size": [640, 480], "bogus": 1}})
    assert cfg.camera.main_size == (640, 480)


def test_roi_to_pixels_clamps():
    roi = cfgmod.Roi(x=0.461, y=0.708, w=0.6, h=0.139)
    x1, y1, x2, y2 = roi.to_pixels(1280, 720)
    assert (x1, x2) == (590, 1280)
    assert 0 <= y1 < y2 <= 720
