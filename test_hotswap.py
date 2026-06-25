"""Integration test: refresh_active_* hot-swaps a running scene in place.

Covers the save-time hot-swap for step motions, generator motions, and looks
(main + effect already had it). Verifies the swap applies live, keeps a single
stack entry per id (no duplicate), and no-ops when the scene isn't running.

Run: python3 test_hotswap.py
"""
import sys, time
sys.path.insert(0, ".")


class StubDMX:
    connected = True
    def __init__(self): self.last_frame = {}
    def set_channels(self, by): self.last_frame = {(u, c): v for u, f in by.items() for c, v in f.items()}
    def connect(self): pass
    def blackout(self): pass
    def get_all_universes_snapshot(self): return {}


from engine import LightingEngine

SHOW = {
    "name": "hotswap", "effect_fade_ms": 10, "singer_fade_ms": 10, "default_launch_fade": 0,
    "fixtures": [
        {"id": "M1", "name": "m1", "type": "mover", "universe": 0, "start_address": 1,
         "channel_roles": {"pan": 1, "pan_fine": 2, "tilt": 3, "tilt_fine": 4, "dimmer": 5,
                           "r": 6, "g": 7, "b": 8}},
    ],
}

def step_motion(name, pan, tilt):
    return {"scene_type": "mover_motion", "name": name,
            "steps": [{"hold": 100000, "fade": 0, "fixtures": {"M1": {"pan": pan, "tilt": tilt}}}]}

def gen_motion(name, shape, cp):
    return {"scene_type": "mover_motion", "motion_mode": "generator", "name": name,
            "tempo_sync": False, "beat_division": 1.0,
            "generator": {"shape": shape, "center_pan": cp, "center_tilt": 128,
                          "size_pan": 0, "size_tilt": 0, "speed": 1.0},  # size 0 => sits at center
            "fixtures": ["M1"], "phase": {"mode": "even", "spread": 1.0}}

def look(name, r, g, b):
    return {"scene_type": "mover_look", "name": name,
            "steps": [{"hold": 100000, "fade": 0, "fixtures": {"M1": {"dimmer": 255, "r": r, "g": g, "b": b}}}]}

PASSED = 0
def ok(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("  ok:", msg)


print("\n[step-motion hot-swap]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_motion_scene(step_motion("s", 100, 50), scene_id="m")
    time.sleep(0.12)
    ok(eng._dmx.last_frame.get((0, 1)) == 100, "initial pan = 100")
    changed = eng.refresh_active_motion("m", step_motion("s", 200, 60))
    time.sleep(0.12)
    ok(changed is True, "refresh reported a live swap")
    ok(eng._dmx.last_frame.get((0, 1)) == 200, "pan hot-swapped to 200")
    ok(len([e for e in eng._active_motions if e["id"] == "m"]) == 1, "still a single entry (no duplicate)")
finally:
    eng.shutdown()


print("\n[generator hot-swap, size 0 so center is exact]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_motion_scene(gen_motion("g", "circle", 100), scene_id="g")
    time.sleep(0.12)
    ok(eng._dmx.last_frame.get((0, 1)) == 100, "generator center_pan = 100")
    th_before = eng._active_motions[0]["thread"]
    c_before = eng._active_motions[0].get("gen_cycles", 0)
    eng.refresh_active_motion("g", gen_motion("g", "circle", 210))
    time.sleep(0.12)
    ok(eng._dmx.last_frame.get((0, 1)) == 210, "generator center hot-swapped to 210")
    ok(len(eng._active_motions) == 1, "single generator entry after swap")
    # Seamless: the player thread is NOT replaced, so the cycle clock keeps
    # running (orbit continues from where it was, no jump to the start).
    ok(eng._active_motions[0]["thread"] is th_before, "gen→gen keeps the same thread (clock preserved)")
    ok(eng._active_motions[0].get("gen_cycles", 0) >= c_before > 0, "cycle clock kept advancing (not reset)")
finally:
    eng.shutdown()

print("\n[mode change restarts in place]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_motion_scene(gen_motion("g", "circle", 100), scene_id="g")
    time.sleep(0.12)
    th_gen = eng._active_motions[0]["thread"]
    eng.refresh_active_motion("g", step_motion("g", 60, 70))   # generator -> steps
    time.sleep(0.12)
    ok(eng._active_motions[0]["thread"] is not th_gen, "gen→step restarts (new thread)")
    ok(eng._dmx.last_frame.get((0, 1)) == 60, "step data applied after mode change")
finally:
    eng.shutdown()


print("\n[look hot-swap]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_look_scene(look("l", 255, 0, 0), scene_id="l")
    time.sleep(0.12)
    ok(eng._dmx.last_frame.get((0, 6)) == 255 and eng._dmx.last_frame.get((0, 8)) == 0, "look red")
    eng.refresh_active_look("l", look("l", 0, 0, 255))
    time.sleep(0.12)
    ok(eng._dmx.last_frame.get((0, 6)) == 0 and eng._dmx.last_frame.get((0, 8)) == 255, "look hot-swapped to blue")
finally:
    eng.shutdown()


print("\n[no-op when not running]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    ok(eng.refresh_active_motion("ghost", step_motion("x", 10, 10)) is False, "motion refresh no-ops when not active")
    ok(eng.refresh_active_look("ghost", look("x", 1, 1, 1)) is False, "look refresh no-ops when not active")
    ok(len(eng._active_motions) == 0 and len(eng._active_looks) == 0, "no stray entries created")
finally:
    eng.shutdown()


print(f"\nALL HOT-SWAP TESTS PASSED ({PASSED} assertions)")
