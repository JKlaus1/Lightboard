"""Functional test for the singer_default_on show-config option.

Verifies:
  1. Absent key defaults to singer_mode=True (unchanged from prior hardcoded
     behavior — no existing show regresses).
  2. Explicit singer_default_on=False boots with singer_mode off, and the
     singer crossfade blend/target start at 0.0 (not 1.0 fading down), so
     there's no visible flash of singer color at boot.
  3. Explicit singer_default_on=True is equivalent to the default.

Run: python3 test_singer_default.py
"""
import sys
sys.path.insert(0, ".")

from engine import LightingEngine


class StubDMX:
    def __init__(self):
        self.connected = True
    def set_channels(self, by_uni): pass
    def connect(self): pass
    def blackout(self): pass
    def get_all_universes_snapshot(self): return {}


BASE_SHOW = {
    "name": "Singer Default Test Show",
    "fixtures": [
        {"id": "par1", "name": "Par 1", "type": "rgbawuv_par",
         "start_address": 1, "channels": 3, "universe": 0,
         "dimmer_channel": 0, "first_pod_channel": 1,
         "channels_per_pod": 3, "pods": 1,
         "pod_color_offsets": {"r": 0, "g": 1, "b": 2},
         "singer_pods": []},
    ],
    "groups": [],
}

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1;  print(f"  ✓ {name}")
    else:    failed += 1;  print(f"  ✗ {name}")


print("1. Absent singer_default_on key -> defaults ON (backward compatible)")
show1 = dict(BASE_SHOW)
eng1 = LightingEngine(StubDMX(), show1)
st1 = eng1.get_state()
check("singer_mode defaults True", st1["singer_mode"] is True)
check("singer_blend starts at 1.0", abs(st1["singer_blend"] - 1.0) < 0.001)
eng1._output_running = False

print("2. singer_default_on=False -> boots with singer OFF, no flash")
show2 = dict(BASE_SHOW); show2["singer_default_on"] = False
eng2 = LightingEngine(StubDMX(), show2)
st2 = eng2.get_state()
check("singer_mode is False", st2["singer_mode"] is False)
check("singer_blend starts at 0.0 (not fading down from 1.0)",
      abs(st2["singer_blend"] - 0.0) < 0.001)
eng2._output_running = False

print("3. singer_default_on=True explicit -> same as default")
show3 = dict(BASE_SHOW); show3["singer_default_on"] = True
eng3 = LightingEngine(StubDMX(), show3)
st3 = eng3.get_state()
check("singer_mode is True", st3["singer_mode"] is True)
check("singer_blend starts at 1.0", abs(st3["singer_blend"] - 1.0) < 0.001)
eng3._output_running = False

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
