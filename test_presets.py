"""Unit tests for presets.py (Task A): capture, additive vs exclusive recall,
scope gating, scalar set-if-scoped, and missing-scene tolerance.

Run: python3 test_presets.py
"""
import sys, time
sys.path.insert(0, ".")

import presets as P
from engine import LightingEngine


class StubDMX:
    connected = True
    def set_channels(self, by_uni): pass
    def connect(self): pass
    def blackout(self): pass
    def get_all_universes_snapshot(self): return {}


SHOW = {
    "name": "Preset Test Show", "default_launch_fade": 0, "effect_fade_ms": 10,
    "fixtures": [
        {"id": "MA", "type": "mover", "universe": 0, "start_address": 1,
         "channel_roles": {"pan": 1, "tilt": 2, "dimmer": 3, "r": 4, "g": 5, "b": 6}},
        {"id": "P", "universe": 0, "start_address": 40, "channels": 7, "pods": 2,
         "dimmer_channel": 1, "first_pod_channel": 2, "channels_per_pod": 3,
         "pod_color_offsets": {"r": 0, "g": 1, "b": 2}},
    ],
}

# A fake library: id -> scene dict, so apply_preset can "load" scenes.
LIB = {
    "S1": {"scene_type": "main", "name": "S1",
           "steps": [{"hold": 100000, "fade": 0, "fixtures": {"P": {"pods": [{"r": 9}, {"r": 9}]}}}]},
    "S2": {"scene_type": "main", "name": "S2",
           "steps": [{"hold": 100000, "fade": 0, "fixtures": {"P": {"pods": [{"g": 9}, {"g": 9}]}}}]},
    "MO1": {"scene_type": "mover_motion", "name": "MO1",
            "steps": [{"hold": 100000, "fade": 0, "fixtures": {"MA": {"pan": 100}}}]},
    "EF1": {"scene_type": "effect", "name": "EF1", "effect": "solid",
            "primary": {"r": 50}, "secondary": {"r": 50}, "params": {},
            "fixtures_enabled": ["P"], "rendering_mode": "continuous_strip", "launch_fade": 0},
}

def loader(sid):
    if sid not in LIB:
        raise FileNotFoundError(sid)
    return LIB[sid]

def wait(s): time.sleep(s)

PASSED = 0
def ok(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("  ok:", msg)

def active(eng):
    s = eng.get_state()
    return (
        {x["id"] for x in s["scenes"]},
        {x["id"] for x in s["motions"]},
        {x["id"] for x in s["effects"] if not x["stopping"]},
    )


# ── 1. capture snapshots the live rig ──────────────────────────────────────
print("\n[capture]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_scene(LIB["S1"], scene_id="S1")
    eng.play_motion_scene(LIB["MO1"], scene_id="MO1")
    eng.set_master(0.5)
    wait(0.1)
    pre = P.capture_preset(eng, name="Verse")
    ok(pre["name"] == "Verse", "capture keeps name")
    ok(pre["items"]["main"] == ["S1"], "capture grabbed main scene")
    ok(pre["items"]["motion"] == ["MO1"], "capture grabbed motion")
    ok(pre["exclusive"] is True, "capture defaults exclusive")
    ok(abs(pre["levels"]["master"] - 0.5) < 1e-6, "capture grabbed master level")
finally:
    eng.shutdown()


# ── 2. additive recall adds without removing ───────────────────────────────
print("\n[additive]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_scene(LIB["S1"], scene_id="S1")
    wait(0.1)
    preset = {"name": "add S2", "exclusive": False,
              "scope": {"main": True}, "items": {"main": ["S2"]}}
    P.apply_preset(preset, eng, loader)
    wait(0.1)
    mains, _, _ = active(eng)
    ok(mains == {"S1", "S2"}, "additive added S2, kept S1")
finally:
    eng.shutdown()


# ── 3. exclusive recall prunes what isn't in the preset ────────────────────
print("\n[exclusive]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_scene(LIB["S1"], scene_id="S1")
    eng.play_motion_scene(LIB["MO1"], scene_id="MO1")
    wait(0.1)
    preset = {"name": "just S2", "exclusive": True,
              "scope": {"main": True}, "items": {"main": ["S2"]}}
    P.apply_preset(preset, eng, loader)
    wait(0.2)
    mains, motions, _ = active(eng)
    ok(mains == {"S2"}, "exclusive replaced S1 with S2")
    ok(motions == {"MO1"}, "unscoped motion left untouched by exclusive")
finally:
    eng.shutdown()


# ── 4. unscoped categories are never touched ───────────────────────────────
print("\n[scope-gating]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.play_scene(LIB["S1"], scene_id="S1")
    wait(0.1)
    # exclusive but main NOT scoped -> S1 must survive even though items.main is empty
    preset = {"name": "motion only", "exclusive": True,
              "scope": {"motion": True}, "items": {"main": [], "motion": ["MO1"]}}
    P.apply_preset(preset, eng, loader)
    wait(0.1)
    mains, motions, _ = active(eng)
    ok(mains == {"S1"}, "unscoped main survived an exclusive recall")
    ok(motions == {"MO1"}, "scoped motion was added")
finally:
    eng.shutdown()


# ── 5. scalars set-if-scoped ───────────────────────────────────────────────
print("\n[scalars]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    eng.set_master(0.2)
    eng.set_singer_mode(True)
    # scope master only; singer_mode level present but NOT scoped -> must not change
    preset = {"name": "dim", "exclusive": False,
              "scope": {"master": True},
              "levels": {"master": 0.8, "singer_mode": False}}
    P.apply_preset(preset, eng, loader)
    wait(0.1)
    s = eng.get_state()
    ok(abs(s["master_level"] - 0.8) < 1e-6, "scoped master applied")
    ok(s["singer_mode"] is True, "unscoped singer_mode left alone")
finally:
    eng.shutdown()


# ── 6. missing scene id is tolerated ───────────────────────────────────────
print("\n[missing-scene]")
eng = LightingEngine(StubDMX(), SHOW)
try:
    preset = {"name": "ghost", "exclusive": False,
              "scope": {"main": True, "effect": True},
              "items": {"main": ["NOPE"], "effect": ["EF1"]}}
    summary = P.apply_preset(preset, eng, loader)
    wait(0.2)
    mains, _, effects = active(eng)
    ok(["main", "NOPE"] in summary["missing"], "missing id reported")
    ok(mains == set(), "missing main id skipped cleanly")
    ok(effects == {"EF1"}, "valid effect still applied despite a missing sibling")
finally:
    eng.shutdown()


# ── 7. normalize fills partial presets ─────────────────────────────────────
print("\n[normalize]")
n = P.normalize_preset({"name": "x"})
ok(set(n["scope"]) == set(P.CATEGORIES) | set(P.SCALARS), "scope filled with all keys")
ok(all(n["items"][c] == [] for c in P.CATEGORIES), "items filled per category")
ok(n["exclusive"] is True, "exclusive defaults true")
PASSED += 0  # (assertions counted in ok)

print("\nALL PRESET TESTS PASSED (%d assertions)" % PASSED)
