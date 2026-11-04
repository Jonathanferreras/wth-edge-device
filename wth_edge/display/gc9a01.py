"""GC9A01 240x240 round SPI display driver (spidev + gpiozero).

Ported from display-wiring/gc9a01_test.py, which was verified on jf-pi-5 with:
VCC pin 1 (3.3 V), GND pin 6, SCL pin 23, SDA pin 19, CS pin 24 (CE0),
DC pin 22 (GPIO 25), RST pin 13 (GPIO 27). Pins come from the profile now.
"""

from __future__ import annotations

import time

import numpy as np

W = H = 240

INIT = [
    (0xEF, []), (0xEB, [0x14]), (0xFE, []), (0xEF, []), (0xEB, [0x14]),
    (0x84, [0x40]), (0x85, [0xFF]), (0x86, [0xFF]), (0x87, [0xFF]),
    (0x88, [0x0A]), (0x89, [0x21]), (0x8A, [0x00]), (0x8B, [0x80]),
    (0x8C, [0x01]), (0x8D, [0x01]), (0x8E, [0xFF]), (0x8F, [0xFF]),
    (0xB6, [0x00, 0x20]),
    # 0x36 memory access control is sent separately from the profile's `madctl`.
    (0x3A, [0x05]),               # 16-bit RGB565
    (0x90, [0x08, 0x08, 0x08, 0x08]), (0xBD, [0x06]), (0xBC, [0x00]),
    (0xFF, [0x60, 0x01, 0x04]), (0xC3, [0x13]), (0xC4, [0x13]), (0xC9, [0x22]),
    (0xBE, [0x11]), (0xE1, [0x10, 0x0E]), (0xDF, [0x21, 0x0C, 0x02]),
    (0xF0, [0x45, 0x09, 0x08, 0x08, 0x26, 0x2A]),
    (0xF1, [0x43, 0x70, 0x72, 0x36, 0x37, 0x6F]),
    (0xF2, [0x45, 0x09, 0x08, 0x08, 0x26, 0x2A]),
    (0xF3, [0x43, 0x70, 0x72, 0x36, 0x37, 0x6F]),
    (0xED, [0x1B, 0x0B]), (0xAE, [0x77]), (0xCD, [0x63]),
    (0x70, [0x07, 0x07, 0x04, 0x0E, 0x0F, 0x09, 0x07, 0x08, 0x03]),
    (0xE8, [0x34]),
    (0x62, [0x18, 0x0D, 0x71, 0xED, 0x70, 0x70, 0x18, 0x0F, 0x71, 0xEF, 0x70, 0x70]),
    (0x63, [0x18, 0x11, 0x71, 0xF1, 0x70, 0x70, 0x18, 0x13, 0x71, 0xF3, 0x70, 0x70]),
    (0x64, [0x28, 0x29, 0xF1, 0x01, 0xF1, 0x00, 0x07]),
    (0x66, [0x3C, 0x00, 0xCD, 0x67, 0x45, 0x45, 0x10, 0x00, 0x00, 0x00]),
    (0x67, [0x00, 0x3C, 0x00, 0x00, 0x00, 0x01, 0x54, 0x10, 0x32, 0x98]),
    (0x74, [0x10, 0x85, 0x80, 0x00, 0x00, 0x4E, 0x00]),
    (0x98, [0x3E, 0x07]),
    (0x35, []),                   # tearing effect on
    (0x21, []),                   # display inversion on (needed for correct colours)
]


def to_rgb565(img) -> bytes:
    a = np.asarray(img.convert("RGB").resize((W, H)), dtype=np.uint16)
    rgb565 = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
    return rgb565.astype(">u2").tobytes()


class GC9A01:
    def __init__(self, cfg) -> None:
        import spidev  # type: ignore[import-not-found]  # apt: python3-spidev
        from gpiozero import DigitalOutputDevice  # apt: python3-gpiozero (+ python3-lgpio on Pi 5)

        self.spi = spidev.SpiDev()
        self.spi.open(cfg.spi_bus, cfg.spi_device)
        self.spi.max_speed_hz = cfg.spi_hz
        self.spi.mode = 0
        self.dc = DigitalOutputDevice(cfg.dc_pin)
        self.rst = DigitalOutputDevice(cfg.rst_pin, initial_value=True)
        self.bl = DigitalOutputDevice(cfg.backlight_pin, initial_value=True) if cfg.backlight_pin >= 0 else None
        self.madctl = cfg.madctl
        self.init()

    def init(self) -> None:
        self.reset()
        for cmd, data in INIT:
            self.command(cmd, data)
        self.command(0x36, [self.madctl])
        self.command(0x11)        # sleep out
        time.sleep(0.12)
        self.command(0x29)        # display on
        time.sleep(0.02)

    def reset(self) -> None:
        self.rst.on(); time.sleep(0.01)
        self.rst.off(); time.sleep(0.01)
        self.rst.on(); time.sleep(0.12)

    def command(self, cmd: int, data=()) -> None:
        self.dc.off()
        self.spi.writebytes([cmd])
        if data:
            self.dc.on()
            self.spi.writebytes2(bytes(data))

    def show(self, img) -> None:
        """Send a 240x240 PIL image."""
        payload = to_rgb565(img)
        self.command(0x2A, [0, 0, 0, W - 1])   # column range
        self.command(0x2B, [0, 0, 0, H - 1])   # row range
        self.command(0x2C)                      # write memory
        self.dc.on()
        self.spi.writebytes2(payload)           # spidev chunks large writes itself

    def close(self) -> None:
        try:
            self.command(0x28)  # display off
        except Exception:
            pass
        self.spi.close()
        self.dc.close()
        self.rst.close()
        if self.bl:
            self.bl.close()


class PngDisplay:
    """Writes each frame to a PNG, for working on the screen layout without a Pi."""

    def __init__(self, cfg) -> None:
        self.path = cfg.png_path

    def show(self, img) -> None:
        tmp = f"{self.path}.tmp.png"
        img.save(tmp)
        import os

        os.replace(tmp, self.path)

    def close(self) -> None:
        pass
