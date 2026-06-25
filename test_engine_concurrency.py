"""Integration test for Task B: concurrency engine upgrade.

Verifies that motion / look / effect are stacked (not single-slot):
  1. Two motions on different movers both composite (union).
  2. Two looks on different movers both composite.
  3. Two effects layer and render independently.
  4. The singer-pod "below" fold reduces to the pre-Task-B math at n=1, and
     an effect on a DIFFERENT fixture does not perturb a singer pod.
  5. Freeze -> tap several layers -> unfreeze applies the diff across all
     three stacks.

Run: python3 test_engine_concurrency.py
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


def wait(seconds):
    time.sleep(seconds)


from engine import LightingEngine


SHOW = {
    "name": "Task B Concurrency Show",
    "effect_fade_ms": 10,
    "singer_fade_ms": 10,
    "default_launch_fade": 0,
    "fixtures": [
        # Two movers (pan/tilt + dimmer/rgb) for motion/look stacking.
        {"id": "MA", "name": "Mover A", "type": "mover", "universe": 0,
         "start_address": 1,
         "channel_roles": {"pan": 1, "tilt": 2, "dimmer": 3, "r": 4, "g": 5, "b": 6}},
        {"id": "MB", "name": "Mover B", "type": "mover", "universe": 0,
         "start_address": 20,
         "channel_roles": {"pan": 1, "tilt": 2, "dimmer": 3, "r": 4, "g": 5, "b": 6}},
        # Pod fixture with a singer pod (pod 1) for the singer-fold test.
        {"id": "P", "name": "Par", "universe": 0, "start_address": 40, "channels": 7,
         "pods": 2, "dimmer_channel": 1, "first_pod_channel": 2, "channels_per_pod": 3,
         "pod_color_offsets": {"r": 0, "g": 1, "b": 2}, "singer_pods": [1]},
        # A bystander pod fixture (no singer pod) for the second effect.
        {"id": "Q", "name": "Par2", "universe": 0, "start_address": 60, "channels": 7,
         "pods": 2, "dimmer_channel": 1, "first_pod_channel": 2, "channels_per_pod": 3,
         "pod_color_offsets": {"r": 0, "g": 1, "b": 2}},
    ],
}


def _motion(name, fxid, pan, tilt):
    return {"scene_type": "mover_motion", "name": name,
            "steps": [{"hold": 100000, "fade": 0, "fixtures": {fxid: {"pan": pan, "tilt": tilt}}}]}


def _look(name, fxid, r, g, b):
    return {"scene_type": "mover_look", "name": name,
            "steps": [{"hold": 100000, "fade": 0,
                       "fixtures": {fxid: {"dimmer": 255, "r": r, "g": g, "b": b}}}]}


def _effect(name, fxids, color):
    return {"scene_type": "effect", "name": name, "effect": "solid",
            "primary": color, "secondary": color, "params": {},
            "fixtures_enabled": fxids, "rendering_mode": "continuous_strip",
            "launch_fade": 0}


def _main(name, fxid="P"):
    return {"scene_type": "main", "name": name,
            "steps": [{"hold": 100000, "fade": 0,
                       "fixtures": {fxid: {"pods": [{"r": 10, "g": 10, "b": 10},
                                                    {"r": 10, "g": 10, "b": 10}]}}}]}


PASSED = 0
def ok(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("  ok:", msg)


# ── 1. Two motions on different movers both composite ──────────────────────
print("\n[two-motions-stack]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_motion_scene(_motion("mA", "MA", 100, 50), scene_id="mA")
    eng.play_motion_scene(_motion("mB", "MB", 200, 111), scene_id="mB")
    wait(0.15)
    f = eng._dmx.last_frame
    ok(f.get((0, 1)) == 100, "MA pan present (=100)")
    ok(f.get((0, 20)) == 200, "MB pan present (=200)")
    st = eng.get_state()
    ok({m["id"] for m in st["motions"]} == {"mA", "mB"}, "both motions in get_state.motions")
    # Stop one; the other survives.
    eng.stop_motion_scene("mA")
    wait(0.1)
    f = eng._dmx.last_frame
    ok(f.get((0, 1), 0) == 0 and f.get((0, 20)) == 200, "stopping mA leaves mB running")
finally:
    eng.shutdown()


# ── 2. Two looks on different movers both composite ────────────────────────
print("\n[two-looks-stack]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_look_scene(_look("lA", "MA", 255, 0, 0), scene_id="lA")
    eng.play_look_scene(_look("lB", "MB", 0, 0, 255), scene_id="lB")
    wait(0.15)
    f = eng._dmx.last_frame
    ok(f.get((0, 4)) == 255, "MA look red present")
    ok(f.get((0, 25)) == 255, "MB look blue present")  # MB start 20, b=offset6 -> ch 25
finally:
    eng.shutdown()


# ── 3. Two effects layer and render independently ──────────────────────────
print("\n[two-effects-stack]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.set_singer_mode(False)            # let effect show through on P's singer pod
    eng.play_effect_scene(_effect("eP", ["P"], {"r": 200, "g": 0, "b": 0}), scene_id="eP")
    eng.play_effect_scene(_effect("eQ", ["Q"], {"r": 0, "g": 0, "b": 150}), scene_id="eQ")
    wait(0.2)
    f = eng._dmx.last_frame
    # P pod 1 r (ch 41) from effect eP; Q pod 1 b (ch 63) from effect eQ.
    ok(f.get((0, 41), 0) > 150, "effect eP lights P (r>150)")
    ok(f.get((0, 63), 0) > 100, "effect eQ lights Q (b>100)")
    st = eng.get_state()
    ok({e["id"] for e in st["effects"]} == {"eP", "eQ"}, "both effects in get_state.effects")
    eng.stop_effect_scene("eP")
    wait(0.2)
    f = eng._dmx.last_frame
    ok(f.get((0, 41), 0) == 0 and f.get((0, 63), 0) > 100, "stopping eP leaves eQ lit")
finally:
    eng.shutdown()


# ── 4. Singer-pod fold: n=1 baseline, and isolation from other-fixture FX ───
print("\n[singer-pod-fold-isolation]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.set_singer_mode(False)            # singer OFF -> singer pod reveals below
    eng.play_effect_scene(_effect("eP", ["P"], {"r": 180, "g": 0, "b": 0}), scene_id="eP")
    wait(0.2)
    p_pod1_r_single = eng._dmx.last_frame.get((0, 41), 0)
    ok(p_pod1_r_single > 150, "singer-off reveals effect on P's singer pod (r>150)")
    # Add an effect on a DIFFERENT fixture; P's singer pod must not change.
    eng.play_effect_scene(_effect("eQ", ["Q"], {"r": 0, "g": 0, "b": 150}), scene_id="eQ")
    wait(0.2)
    p_pod1_r_double = eng._dmx.last_frame.get((0, 41), 0)
    ok(p_pod1_r_double == p_pod1_r_single,
       "effect on Q does not perturb P's singer pod (%d==%d)" % (p_pod1_r_double, p_pod1_r_single))
    # Singer ON -> singer color wins over the effect on the singer pod.
    eng.set_singer_mode(True)
    wait(0.2)
    # The singer pod should now track the singer color, not the 180 effect red.
    ok(eng._dmx.last_frame.get((0, 41), 0) != p_pod1_r_single,
       "singer-on overrides effect on the singer pod")
finally:
    eng.shutdown()


# ── 5. Freeze -> tap across layers -> unfreeze diff ────────────────────────
print("\n[freeze-unfreeze-diff]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_scene(_main("S1"), scene_id="S1")
    eng.play_motion_scene(_motion("MO1", "MA", 10, 10), scene_id="MO1")
    wait(0.1)
    eng.set_freeze(True)
    # Queue: add a 2nd main, a 2nd motion, and an effect; all via toggle.
    eng.play_scene(_main("S2", "Q"), scene_id="S2")          # queued add
    eng.toggle_motion_scene(_motion("MO2", "MB", 20, 20), scene_id="MO2")
    eng.toggle_effect_scene(_effect("E1", ["P"], {"r": 50, "g": 0, "b": 0}), scene_id="E1")
    st = eng.get_state()
    ok(st["freeze"]["active"], "freeze active")
    ok(set(st["freeze"]["pending_main_ids"]) == {"S1", "S2"}, "pending mains queued")
    ok(set(st["freeze"]["pending_motion_ids"]) == {"MO1", "MO2"}, "pending motions queued")
    ok(set(st["freeze"]["pending_effect_ids"]) == {"E1"}, "pending effect queued")
    # Live state still unchanged while frozen.
    ok({m["id"] for m in st["motions"]} == {"MO1"}, "live motions unchanged while frozen")
    eng.set_freeze(False)
    wait(0.2)
    st = eng.get_state()
    ok({s["id"] for s in st["scenes"]} == {"S1", "S2"}, "unfreeze applied main add")
    ok({m["id"] for m in st["motions"]} == {"MO1", "MO2"}, "unfreeze applied motion add")
    ok({e["id"] for e in st["effects"] if not e["stopping"]} == {"E1"}, "unfreeze applied effect add")
finally:
    eng.shutdown()


# ── 6. clear_all() panic reset ─────────────────────────────────────────────
print("\n[clear-all-panic-reset]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_scene(_main("S1"), scene_id="S1")
    eng.play_motion_scene(_motion("MO1", "MA", 10, 10), scene_id="MO1")
    eng.play_look_scene(_look("LO1", "MB", 1, 2, 3), scene_id="LO1")
    eng.play_effect_scene(_effect("E1", ["P"], {"r": 50, "g": 0, "b": 0}), scene_id="E1")
    eng.set_master(0.4)
    eng.set_singer_level(0.5)
    eng.set_singer_mode(True)
    wait(0.15)
    eng.clear_all()
    wait(0.25)   # let graceful fades complete
    st = eng.get_state()
    ok(st["scenes"] == [], "clear_all stopped all main scenes")
    ok(st["motions"] == [], "clear_all stopped all motions")
    ok(st["looks"] == [], "clear_all stopped all looks")
    ok([e for e in st["effects"] if not e["stopping"]] == [], "clear_all stopped all effects")
    ok(abs(st["master_level"] - 1.0) < 1e-6, "clear_all reset master to 100%")
    ok(abs(st["singer_level"] - 1.0) < 1e-6, "clear_all reset singer dimmer to 100%")
    ok(st["singer_mode"] is False, "clear_all turned singer mode OFF")
    ok(st["blackout_mode"] in (None, "off") or st["blackout_blend"] == 0.0, "clear_all cleared blackout")
    # Bypasses freeze: clear while frozen still applies.
    eng.play_scene(_main("S2"), scene_id="S2")
    wait(0.1)
    eng.set_freeze(True)
    eng.clear_all()
    wait(0.2)
    st = eng.get_state()
    ok(not st["freeze"]["active"], "clear_all dropped freeze")
    ok(st["scenes"] == [], "clear_all cleared scenes despite freeze")
finally:
    eng.shutdown()


print("\nALL CONCURRENCY TESTS PASSED (%d assertions)" % PASSED)