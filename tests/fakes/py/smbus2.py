"""Fake smbus2 that answers like the X120x MAX17048 fuel gauge at 0x36.

Values come from $FAKE_STATE/hw.json, so a test can change the battery level
while the script is running."""
import json
import os

_HW = os.path.join(os.environ["FAKE_STATE"], "hw.json")


def _swap(w):
    return ((w & 0xFF) << 8) | (w >> 8)  # SMBus words are little-endian


class SMBus:
    def __init__(self, bus):
        self.bus = bus

    def read_word_data(self, addr, reg):
        with open(_HW) as f:
            hw = json.load(f)
        if addr != 0x36 or not hw.get("ups", True):
            raise OSError(121, "Remote I/O error")
        if reg == 0x02:    # VCELL, 78.125 uV per bit
            w = round(hw["volt"] / 78.125e-6)
        elif reg == 0x04:  # SOC, 1/256 % per bit
            w = round(hw["soc"] * 256)
        elif reg == 0x08:  # VERSION: MAX17048 = 0x001X
            w = 0x0012
        elif reg == 0x16:  # CRATE, 0.208 %/h per bit, signed
            w = round(hw.get("crate", 0) / 0.208) & 0xFFFF
        else:
            w = 0
        return _swap(w & 0xFFFF)

    def close(self):
        pass
