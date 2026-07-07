"""Functional test for the Phase 1 custom fader system.

Verifies:
  1. Intensity resolution — hardware dimmer vs dimmerless (color surface) vs mover
  2. Group + fixture target union with dedupe
  3. Explicit channel offsets (out-of-range ignored)
  4. Limit mode multiplies engine output; full = invisible
  5. Override disarmed = no effect; armed = stamps value, beats blackout
  6. Level/arm state survives set_custom_faders() reconfig and load_show()
  7. get_state carries faders

Run: python3 test_faders.py
"""
import sys, time
sys.path.insert(0, ".")


class StubDMX:
    def __init__(self):
        self.connected = True
        self.last_frame = {}
    def set_channels(self, by_uni):
        self.last_frame = {(u, ch): v for u, frame in by_uni.items()
                           for ch, v in frame.items()}
    def connect(self): pass
    def blackout(self): pass
    def get_all_universes_snapshot(self): return {}


from engine import LightingEngine

SHOW = {
    "name": "Fader Test Show",
    "singer_fade_ms": 10,
    "blackout_fade_ms": 10,
    "fixtures": [
        {   # EXA-style bar: hardware dimmer at offset 1, 2 pods RGB
            "id": "bar1", "name": "Bar 1", "type": "pod",
            "start_address": 1, "channels": 8, "universe": 0,
            "dimmer_channel": 1, "first_pod_channel": 2,
            "channels_per_pod": 3, "pods": 2,
            "pod_color_offsets": {"r": 0, "g": 1, "b": 2},
            "singer_pods": [],
        },
        {   # Betopper-style par: NO dimmer, 1 pod RGB
            "id": "par1", "name": "Par 1", "type": "rgbawuv_par",
            "start_address": 20, "channels": 3, "universe": 0,
            "dimmer_channel": 0, "first_pod_channel": 1,
            "channels_per_pod": 3, "pods": 1,
            "pod_color_offsets": {"r": 0, "g": 1, "b": 2},
            "singer_pods": [],
        },
        {   # Mover with dimmer role
            "id": "mv1", "name": "Mover 1", "type": "mover",
            "start_address": 100, "channels": 10, "universe": 0,
            "channel_roles": {"pan": 1, "tilt": 2, "dimmer": 5},
        },
    ],
    "groups": [
        {"id": "g1", "name": "Towers", "members": ["bar1", "par1"]},
    ],
}

PASS = 0
def check(name, cond, detail=""):
    global PASS
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if cond:
        PASS += 1
    else:
        sys.exit(1)


dmx = StubDMX()
eng = LightingEngine(dmx, SHOW)
time.sleep(0.2)

# ── 1. Resolution ──────────────────────────────────────────────────────────
eng.set_custom_faders([
    {"id": "fA", "mode": "limit", "channels": "intensity",
     "targets": {"groups": ["g1"], "fixtures": ["bar1", "mv1"]}},   # bar1 deduped
    {"id": "fB", "mode": "override", "channels": [1, 2, 99],
     "targets": {"fixtures": ["par1"]}},
])
with eng._lock:
    kA = eng._custom_faders["fA"]["keys"]
    kB = eng._custom_faders["fB"]["keys"]

# fA intensity: bar1 dimmer ch1; par1 (no dimmer) → colors 20,21,22; mv1 dimmer role 5 → abs 104
check("intensity: bar dimmer",   (0, 1) in kA)
check("intensity: bar colors excluded", (0, 2) not in kA and (0, 5) not in kA)
check("intensity: dimmerless par -> color surface",
      {(0, 20), (0, 21), (0, 22)} <= kA)
check("intensity: mover dimmer role", (0, 104) in kA)
check("intensity: dedupe/union size", len(kA) == 5, f"got {sorted(kA)}")
# fB explicit offsets 1,2 within par1 (3ch); 99 out of range ignored
check("explicit offsets", kB == frozenset({(0, 20), (0, 21)}), f"got {sorted(kB)}")

# ── 2. Limit behavior ──────────────────────────────────────────────────────
# Play a "scene" via preview override: par1 red full, bar1 pod1 red full
eng._preview_active = True
eng._preview_dmx = {(0, 2): 255.0, (0, 20): 255.0, (0, 21): 255.0, (0, 22): 255.0}
time.sleep(0.15)
f = dmx.last_frame
check("limit @ full is invisible: dimmer at 255", f.get((0, 1)) == 255)
check("limit @ full is invisible: par color", f.get((0, 20)) == 255)

eng.set_fader_level("fA", 0.5)
time.sleep(0.15)
f = dmx.last_frame
check("limit 50%: bar dimmer scaled", f.get((0, 1)) == 128, f"got {f.get((0,1))}")
check("limit 50%: bar COLOR untouched", f.get((0, 2)) == 255, f"got {f.get((0,2))}")
check("limit 50%: par colors scaled", f.get((0, 20)) == 128, f"got {f.get((0,20))}")
check("limit 50%: mover dimmer scaled (from 0)", f.get((0, 104)) == 0)

# ── 3. Override behavior ───────────────────────────────────────────────────
eng.set_fader_level("fB", 0.0)   # parked at 0, but DISARMED
time.sleep(0.15)
f = dmx.last_frame
check("override disarmed does nothing", f.get((0, 20)) == 128, f"got {f.get((0,20))}")

eng.set_fader_armed("fB", True)
time.sleep(0.15)
f = dmx.last_frame
check("override armed @0 kills ch 20", f.get((0, 20)) == 0)
check("override armed @0 kills ch 21", f.get((0, 21)) == 0)
check("override leaves ch 22 alone (limit only)", f.get((0, 22)) == 128, f"got {f.get((0,22))}")

eng.set_fader_level("fB", 1.0)
time.sleep(0.15)
f = dmx.last_frame
check("override armed @full forces 255", f.get((0, 20)) == 255)

# Override beats blackout
eng.blackout("full")
time.sleep(0.3)
f = dmx.last_frame
check("blackout kills non-overridden", f.get((0, 2)) == 0, f"got {f.get((0,2))}")
check("armed override beats blackout", f.get((0, 20)) == 255, f"got {f.get((0,20))}")
eng.blackout_release() if hasattr(eng, "blackout_release") else None

# ── 4. State preservation across reconfig and show swap ───────────────────
eng.set_fader_level("fA", 0.25)
eng.set_custom_faders([
    {"id": "fA", "mode": "limit", "channels": "intensity",
     "targets": {"groups": ["g1"]}},
])
st = {s["id"]: s for s in eng.get_fader_state()}
check("level survives reconfig", abs(st["fA"]["level"] - 0.25) < 1e-6)
check("removed fader gone", "fB" not in st)

eng.load_show(dict(SHOW))
st = {s["id"]: s for s in eng.get_fader_state()}
check("level survives load_show", abs(st["fA"]["level"] - 0.25) < 1e-6)
with eng._lock:
    check("keys re-resolved after load_show", len(eng._custom_faders["fA"]["keys"]) == 4)

full = eng.get_state()
check("get_state carries faders", any(x["id"] == "fA" for x in full.get("faders", [])))

# ── 8. System faders (Master / Singer dimmer) ────────────────────────
eng.set_custom_faders([
    {"id": "sysM", "label": "MASTER", "system": "master", "mode": "override"},
    {"id": "sysS", "label": "SINGER", "system": "singer", "mode": "override"},
])
with eng._lock:
    check("system fader resolves to no DMX channels",
          eng._custom_faders["sysM"]["keys"] == frozenset() and
          eng._custom_faders["sysS"]["keys"] == frozenset())
st = {s["id"]: s for s in eng.get_fader_state()}
check("get_fader_state carries system marker",
      st["sysM"]["system"] == "master" and st["sysS"]["system"] == "singer")

eng.set_fader_level("sysM", 0.4)
check("master fader drives _master_level", abs(eng._master_level - 0.4) < 1e-6)
st = {s["id"]: s for s in eng.get_fader_state()}
check("master fader state mirrors scalar", abs(st["sysM"]["level"] - 0.4) < 1e-6)

eng.set_fader_level("sysS", 0.6)
check("singer fader drives _singer_level", abs(eng._singer_level - 0.6) < 1e-6)
st = {s["id"]: s for s in eng.get_fader_state()}
check("singer fader state mirrors scalar", abs(st["sysS"]["level"] - 0.6) < 1e-6)

eng.set_master(1.0)   # e.g. Show Board slider / clear-all
st = {s["id"]: s for s in eng.get_fader_state()}
check("master fader follows external scalar change", abs(st["sysM"]["level"] - 1.0) < 1e-6)

print(f"\nAll {PASS} fader checks passed.")
