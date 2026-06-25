"""Integration test: generator-mode mover_motion runs through the live engine.

Confirms the procedural generator publishes through entry['dmx'] exactly like a
step motion — composited to DMX output, stackable, stoppable — and that it
actually moves continuously over time (16-bit pan/tilt populated, value changes
tick-to-tick). Pure wall-clock path (no tap tempo needed).

Run: python3 test_mover_gen_engine.py
"""
import sys, time
sys.path.insert(0, ".")


class StubDMX:
    def __init__(self):
        self.connected = True
        self.last_frame = {}
    def set_channels(self, by_uni):
        self.last_frame = {(u, ch): v for u, frame in by_uni.items() for ch, v in frame.items()}
    def connect(self): pass
    def blackout(self): pass
    def get_all_universes_snapshot(self): return {}


from engine import LightingEngine

SHOW = {
    "name": "Generator Show",
    "effect_fade_ms": 10, "singer_fade_ms": 10, "default_launch_fade": 0,
    "fixtures": [
        # 16-bit mover: pan/pan_fine/tilt/tilt_fine + dimmer.
        {"id": "M16", "name": "Mover 16bit", "type": "mover", "universe": 0,
         "start_address": 1,
         "channel_roles": {"pan": 1, "pan_fine": 2, "tilt": 3, "tilt_fine": 4, "dimmer": 5}},
        # 8-bit mover: pan/tilt only (no fine) — fine roles must be dropped.
        {"id": "M8", "name": "Mover 8bit", "type": "mover", "universe": 0,
         "start_address": 20,
         "channel_roles": {"pan": 1, "tilt": 2, "dimmer": 3}},
    ],
}


def gen_scene(name, fixtures, shape="circle", speed=1.0, phase_mode="even"):
    return {
        "scene_type": "mover_motion", "motion_mode": "generator", "name": name,
        "tempo_sync": False, "beat_division": 1.0,
        "generator": {"shape": shape, "center_pan": 128, "center_tilt": 128,
                      "size_pan": 100, "size_tilt": 100, "speed": speed},
        "fixtures": fixtures,
        "phase": {"mode": phase_mode, "spread": 1.0},
    }


PASSED = 0
def ok(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("  ok:", msg)


print("\n[generator-runs-and-moves]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_motion_scene(gen_scene("circle", ["M16", "M8"], speed=1.0), scene_id="g1")
    time.sleep(0.1)
    f1 = dict(eng._dmx.last_frame)
    # 16-bit mover: pan coarse(ch1)+fine(ch2)+tilt coarse(ch3)+fine(ch4) all present & in range.
    for ch in (1, 2, 3, 4):
        ok((0, ch) in f1 and 0 <= f1[(0, ch)] <= 255, f"M16 ch{ch} present & in range")
    # 8-bit mover: pan(ch20)+tilt(ch21) present; NO fine channels written beyond its map.
    ok((0, 20) in f1 and (0, 21) in f1, "M8 pan+tilt present")

    st = eng.get_state()
    ok([m["id"] for m in st["motions"]] == ["g1"], "generator appears as a normal motion chip")

    # It moves: sample again after time advances; pan 16-bit value should differ.
    def pan16(f, base):
        return f.get((0, base), 0) * 256 + f.get((0, base + 1), 0)
    p_a = pan16(f1, 1)
    time.sleep(0.25)
    f2 = dict(eng._dmx.last_frame)
    p_b = pan16(f2, 1)
    ok(p_a != p_b, f"M16 pan advances over time ({p_a} -> {p_b})")

    # Stop it: motion clears.
    eng.stop_motion_scene("g1")
    time.sleep(0.1)
    ok(len(eng.get_state()["motions"]) == 0, "stopping generator clears the motion stack")
finally:
    eng.shutdown()


print("\n[generator-stacks-with-step-motion]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    # A generator on M16 and a plain step motion on M8 coexist (Task B stacking).
    eng.play_motion_scene(gen_scene("g", ["M16"], speed=1.0), scene_id="gen")
    step = {"scene_type": "mover_motion", "name": "step",
            "steps": [{"hold": 100000, "fade": 0, "fixtures": {"M8": {"pan": 77, "tilt": 88}}}]}
    eng.play_motion_scene(step, scene_id="stp")
    time.sleep(0.15)
    f = dict(eng._dmx.last_frame)
    ok(f.get((0, 20)) == 77 and f.get((0, 21)) == 88, "step motion on M8 holds (77/88)")
    ok((0, 1) in f, "generator on M16 runs alongside")
    ok({m["id"] for m in eng.get_state()["motions"]} == {"gen", "stp"}, "both motions stacked")
finally:
    eng.shutdown()


print(f"\nALL ENGINE GENERATOR TESTS PASSED ({PASSED} assertions)")
