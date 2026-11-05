import threading

from PIL import Image

from wth_edge.cli import main
from wth_edge.display.art import kit, scenes
from wth_edge.display.art.player import available, load, play


class FakeDisplay:
    def __init__(self):
        self.frames = []

    def show(self, img):
        self.frames.append(img)

    def close(self):
        pass


def test_every_scene_renders_and_bundled_gifs_exist():
    assert set(available()) >= set(scenes.SCENES)
    for name, fn in scenes.SCENES.items():
        frames, durs, _loop = fn()
        assert frames and len(frames) == len(durs), name
        assert all(f.size == (kit.LOFI_SIZE, kit.LOFI_SIZE) for f in frames)
        big = kit.finish(frames[0])
        assert big.size == (240, 240)
        assert big.getpixel((0, 0)) == (0, 0, 0)  # outside the circle is masked


def test_build_and_load_roundtrip(tmp_path):
    (path,) = scenes.build(tmp_path, ["idle"])
    anim = load(path)
    assert anim.loop and len(anim.frames) == len(scenes.idle()[0])
    assert all(f.size == (240, 240) for f in anim.frames)
    boot = load("boot")
    assert not boot.loop and 2 < boot.length_s < 10


def test_small_and_folder_frames_are_scaled_up(tmp_path):
    for k in range(3):
        Image.new("RGB", (80, 80), (k * 50, 0, 0)).save(tmp_path / f"{k:03}.png")
    anim = load(tmp_path)
    assert len(anim.frames) == 3 and anim.frames[0].size == (240, 240)


def test_play_respects_loops_and_stop():
    anim = load("alert")
    dev = FakeDisplay()
    assert play(dev, anim, loops=2, speed=1000) == 2 * len(anim.frames)
    stop = threading.Event()
    stop.set()
    assert play(FakeDisplay(), anim, loops=0, stop=stop) == 0


def test_finish_scales_80_and_120_canvases_to_the_panel():
    assert kit.finish(Image.new("RGB", (80, 80))).size == (240, 240)
    assert kit.finish(Image.new("RGB", (120, 120))).size == (240, 240)


def test_lofi_scene_states_draw_and_differ():
    from wth_edge.display.art import lofi

    base = lofi.draw(3)
    assert base.size == (kit.LOFI_SIZE, kit.LOFI_SIZE)
    looks = [{"lift": 1.0, "traffic": "standstill", "wait": "WAIT 4M"}, {"boat_x": 40}, {"night": True},
             {"fog": 0.9, "lowvis": True}, {"snow": True}, {"offline": True},
             {"cam_ok": False}, {"brownout": True, "hot": True, "rec": True}, {"traffic": "unknown"}]
    seen = {base.tobytes()}
    for look in looks:
        img = lofi.draw(3, look)
        assert img.tobytes() not in seen, look
        seen.add(img.tobytes())
    assert lofi.draw(12, {"storm": True}).tobytes() != lofi.draw(12).tobytes()  # lightning fires every 12 ticks


def test_sprites_only_use_the_palette():
    kit.Sprite("K.W\nPCE")
    try:
        kit.Sprite("KZ")
    except ValueError:
        return
    raise AssertionError("unknown colour accepted")


def test_art_list_cli(capsys):
    assert main(["art", "list"]) == 0
    assert "boot" in capsys.readouterr().out


def test_live_scene_draws_every_demo_state():
    import time as _t

    from wth_edge.display.art.live import DEMOS, LiveScene

    for name, (state, status, age, hour) in DEMOS.items():
        scene = LiveScene()
        now = _t.mktime((2026, 11, 4, hour, 0, 0, 0, 0, -1))
        frames = [scene.frame(state, status, age, now) for _ in range(30)]
        assert all(f.size == (240, 240) for f in frames), name
        assert frames[0].tobytes() != frames[5].tobytes(), name  # it animates
    blank = LiveScene().frame(None, "waiting")
    assert blank.size == (240, 240)


def test_ticker_items_follow_telemetry():
    from wth_edge.display.art.live import DEMOS, ticker_items

    state = DEMOS["live-hot-lowpower"][0]
    texts = [t for t, _ in ticker_items(state, "degraded", 1.0, "C")]
    assert "82C" in texts and "4.71V" in texts and "LOW PWR" in texts and "15FPS" in texts
    cam_down = [t for t, _ in ticker_items(DEMOS["live-camera-down"][0], "degraded", 1.0)]
    assert "CAM DOWN" in cam_down
    boat = [t for t, _ in ticker_items(DEMOS["live-boat"][0], "ok", 1.0)]
    assert "BOAT W-E" in boat and "BRIDGE OPEN" in boat and "REC" in boat
    assert ticker_items(None, "offline", 40.0)[0][0] == "OFFLINE"
    fog = [t for t, _ in ticker_items(DEMOS["live-fog"][0], "ok", 1.0)]
    assert "VIS 600M" in fog


def test_live_look_follows_state_and_weather():
    from wth_edge.display.art.live import DEMOS, LiveScene, weather_look

    scene = LiveScene()
    noon = 1_700_000_000.0
    ok = scene.look(DEMOS["live-ok"][0], "ok", noon)
    assert ok["traffic"] == "flowing" and ok["density"] == 0.55 and ok["wait"] == "" and ok["rain"] == 1.0
    scene.lift = 1.0
    boat = scene.look(DEMOS["live-boat"][0], "ok", noon)
    assert boat["traffic"] == "standstill" and boat["wait"] == "WAIT 4M" and boat["boat_x"] is not None
    assert boat["rec"] and boat["lift"] == 1.0
    off = LiveScene().look(DEMOS["live-offline"][0], "offline", noon)
    assert off["offline"] and off["traffic"] == "unknown" and off["ring"] == "R"
    fog = weather_look({"ok": True, "kind": "fog", "precipitation_mm": 0.0, "visibility_m": 600})
    assert fog["lowvis"] and fog["fog"] > 0.9 and fog["rain"] == 0
    assert weather_look({"ok": True, "kind": "heavy_rain", "precipitation_mm": 7.0})["rain"] == 2
    assert weather_look({"ok": False})["rain"] == 1.0 and not weather_look(None)["lowvis"]


def test_bridge_eases_to_its_real_position():
    from wth_edge.display.art.live import DEMOS, LiveScene

    scene = LiveScene()
    state = DEMOS["live-boat"][0]
    for _ in range(60):
        scene.frame(state, "ok", 1.0)
    assert scene.lift == 1.0
    closed = DEMOS["live-ok"][0]
    for _ in range(60):
        scene.frame(closed, "ok", 1.0)
    assert scene.lift == 0.0


def test_alternate_mode_schedule():
    from wth_edge.display.service import art_turn

    assert art_turn("art", 5, 20, 6) and not art_turn("status", 5, 20, 6)
    assert art_turn("alternate", 26 * 10 + 3, 20, 6)
    assert not art_turn("alternate", 26 * 10 + 22, 20, 6)


def test_display_service_runs_in_art_mode(tmp_path):
    import threading as _th

    from wth_edge import config as config_mod
    from wth_edge.display.service import run_display

    cfg = config_mod.Config()
    cfg.display.driver = "png"
    cfg.display.png_path = str(tmp_path / "screen.png")
    cfg.display.mode = "art"
    cfg.display.boot_animation = ""
    cfg.paths.state_file = str(tmp_path / "state.json")
    stop = _th.Event()
    timer = _th.Timer(0.6, stop.set)
    timer.start()
    assert run_display(cfg, stop) == 0
    assert Image.open(tmp_path / "screen.png").size == (240, 240)
