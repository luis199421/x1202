"""Shared state for the fake Raspberry Pi tools (test use only)."""
import json
import os
import sys

STATE = os.environ["FAKE_STATE"]
HW = os.path.join(STATE, "hw.json")


def load():
    with open(HW) as f:
        return json.load(f)


def save(hw):
    tmp = HW + ".tmp"
    with open(tmp, "w") as f:
        json.dump(hw, f)
    os.replace(tmp, HW)


def log_call(name):
    with open(os.path.join(STATE, "calls.log"), "a") as f:
        f.write(name + " " + " ".join(sys.argv[1:]) + "\n")
