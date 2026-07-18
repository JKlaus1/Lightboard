"""Regression test for the clear_all() flash-bang bug.

Scenario: a scene is running with the master (color dimmer) pulled down to,
say, 40%. Operator hits Clear All. The OLD behavior jammed the master to
100% immediately while the scene was still mid-fade-out — so for a split
second the still-lit scene was multiplied by 1.0 instead of 0.4 and the
room flashed brighter right as you were trying to kill it.

The fix defers the master/singer dimmer restore until the visible output has
actually faded to black. This test watches the actual DMX frames written to
the driver across the whole clear_all() fade and asserts NO frame ever
exceeds the brightness that was on screen the instant before the clear. It
then confirms the levels do end up restored to 100% once everything's dark.

Run: python3 test_clear_all_flash.py
"""
import sys, time, threading
sys.path.insert(0, ".")

from engine import LightingEngine


class RecordingDMX:
    """Captures every frame written, and (optionally) the running max of any
    channel value seen, so we can prove no mid-clear brightness spike."""
    def __init__(self):
        self.connected = True
        self.frames = []
        self.watching = False
        self.peak_while_watching = 0
        self._lock = threading.Lock()
    def set_channels(self, by_uni):
        peak = 0
        for u, frame in by_uni.items():
            for ch, v in frame.items():
                if v > peak:
                    peak = v
        with self._lock:
            if self.watching and peak > self.peak_while_watching:
                self.peak_while_watching = peak
        self.frames.append(by_uni)
    def connect(self): pass
    def blackout(self): pass
    def get_all_universes_snapshot(self): return {}


# Single par, one RGB pod. Long-ish scene fade so the flash window (if the bug
# were present) would be wide and easy to catch.
SHOW = {
    "name": "Flash Test Show",
    "singer_fade_ms": 10,
    "blackout_fade_ms": 10,
    "fixtures": [
        {"id": "par1", "name": "Par 1", "type": "rgbawuv_par",
         "start_address": 1, "channels": 3, "universe": 0,
         "dimmer_channel": 0, "first_pod_channel": 1,
         "channels_per_pod": 3, "pods": 1,
         "pod_color_offsets": {"r": 0, "g": 1, "b": 2},
         "singer_pods": []},
    ],
    "groups": [],
    "singer_default_on": False,   # keep singer out of this test entirely
}

# A full-white static scene at full internal level. 600ms fade-out so the
# clear's fade is clearly observable over many output ticks.
SCENE = {
    "id": "flashscene",
    "name": "Full White",
    "scene_type": "main",
    "fade": 600,
    "steps": [
        {"duration": 1000, "fixtures": {
            "par1": {"pods": [{"r": 255, "g": 255, "b": 255}]}
        }},
    ],
}

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1;  print(f"  ✓ {name}")
    else:    failed += 1;  print(f"  ✗ {name}")


print("clear_all() flash-bang regression")
dmx = RecordingDMX()
eng = LightingEngine(dmx, SHOW)

# Start the scene and let it fade fully in.
eng.play_scene(SCENE, scene_id="flashscene")
time.sleep(0.8)

# Pull the master down to 40% and let it settle.
eng.set_master(0.4)
time.sleep(0.3)

# Sample the on-screen peak right before the clear — this is the ceiling no
# subsequent frame is allowed to exceed.
pre_peak = 0
for _ in range(6):
    by_uni = dmx.frames[-1]
    for u, frame in by_uni.items():
        for ch, v in frame.items():
            pre_peak = max(pre_peak, v)
    time.sleep(1.0 / eng.OUTPUT_HZ)
check(f"scene visibly dimmed before clear (peak={pre_peak}, expected ~102)",
      80 <= pre_peak <= 120)

# Arm the watcher and fire the clear. Allow a small tolerance for rounding in
# the composite math (±3/255) — the bug produced a jump to ~255, so this is a
# night-and-day distinction, not a hair-splitting threshold.
dmx.peak_while_watching = 0
dmx.watching = True
eng.clear_all()
# Watch across the entire fade-out plus the deferred-reset settle.
time.sleep(1.5)
dmx.watching = False

check(f"no brightness spike during clear (peak-during={dmx.peak_while_watching}, "
      f"ceiling={pre_peak + 3})",
      dmx.peak_while_watching <= pre_peak + 3)

# After everything's dark, output should be fully black...
time.sleep(0.3)
last = dmx.frames[-1]
post_peak = max((v for frame in last.values() for v in frame.values()), default=0)
check(f"output is dark after clear (peak={post_peak})", post_peak <= 2)

# ...and the master/singer levels should have been restored to 100%.
st = eng.get_state()
check("master restored to 100% after output dark",
      abs(st["master_level"] - 1.0) < 0.01)
check("singer_level restored to 100% after output dark",
      abs(st["singer_level"] - 1.0) < 0.01)

eng._output_running = False
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
