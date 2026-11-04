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
        assert all(f.size == (kit.SIZE, kit.SIZE) for f in frames)
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
