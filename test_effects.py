"""Unit tests for effects.py. Run:
    python3 test_effects.py
Exits non-zero on first failure."""

import sys, math
sys.path.insert(0, ".")
import effects as fx


# Test colors
RED   = {"r": 255, "g": 0,   "b": 0,   "w": 0}
GREEN = {"r": 0,   "g": 255, "b": 0,   "w": 0}
BLUE  = {"r": 0,   "g": 0,   "b": 255, "w": 0}


def approx(a, b, eps=2):
    return abs(a - b) <= eps


# ── 1. SOLID ───────────────────────────────────────────────────────────────
out = fx.render_solid(8, 0.0, {"primary": RED}, ("r","g","b","w"))
assert len(out) == 8
assert all(c == RED for c in out), "All cells must equal primary"
# Solid is time-invariant
assert fx.render_solid(8, 100.0, {"primary": RED}, ()) == out
# Edge case: empty strip
assert fx.render_solid(0, 0.0, {"primary": RED}, ()) == []
print("✓ solid: length, uniformity, time-invariance, empty-strip")


# ── 2. BREATHE ─────────────────────────────────────────────────────────────
# At t=0, brightness should be 0 (or floor)
out0 = fx.render_breathe(4, 0.0, {"primary": RED, "speed": 1.0, "floor": 0.0}, ())
assert out0[0]["r"] == 0, f"breathe at t=0 should be dark, got {out0[0]}"
# All cells identical (breathe is global)
assert all(c == out0[0] for c in out0)
# At t = half period of speed=1Hz (= 0.5s), brightness should peak (=255)
out_peak = fx.render_breathe(4, 0.5, {"primary": RED, "speed": 1.0, "floor": 0.0}, ())
assert approx(out_peak[0]["r"], 255), f"breathe peak should be 255, got {out_peak[0]['r']}"
# Floor respected
out_floor = fx.render_breathe(4, 0.0, {"primary": RED, "speed": 1.0, "floor": 0.5}, ())
assert approx(out_floor[0]["r"], 127), f"floor=0.5 should give 127 at min, got {out_floor[0]['r']}"
# Determinism
assert fx.render_breathe(4, 1.234, {"primary": RED, "speed": 0.7}, ()) \
    == fx.render_breathe(4, 1.234, {"primary": RED, "speed": 0.7}, ())
print("✓ breathe: dark at t=0, peaks at half-period, floor respected, deterministic")


# ── 3. CHASE ───────────────────────────────────────────────────────────────
# At t=0, head at cell 0 → cell 0 brightest
params = {"primary": RED, "secondary": fx.BLACK, "speed": 1.0, "size": 1}
out = fx.render_chase(8, 0.0, params, ())
assert len(out) == 8
assert out[0] == RED, f"chase t=0 cell 0 should be primary, got {out[0]}"
# Adjacent cells should be dark (size=1, integer position)
assert out[1]["r"] == 0
assert out[7]["r"] == 0
# At t = 0.5 / speed / strip_length (=0.0625), head should be between cells 0 and 1
# but head = (0.0625 * 1 * 8) % 8 = 0.5. Cell 0 and 1 should each be ~50% bright.
out_mid = fx.render_chase(8, 0.0625, params, ())
assert approx(out_mid[0]["r"], 127, eps=5), f"cell 0 should be ~50% at half-cell, got {out_mid[0]['r']}"
assert approx(out_mid[1]["r"], 127, eps=5), f"cell 1 should be ~50% at half-cell, got {out_mid[1]['r']}"
# Larger size gives wider peak
params2 = {"primary": RED, "secondary": fx.BLACK, "speed": 1.0, "size": 3}
out_wide = fx.render_chase(8, 0.0, params2, ())
# cell 0 full, cell 1 = 1 - 1/3 = 0.67 → ~170
assert out_wide[0] == RED
assert approx(out_wide[1]["r"], 170, eps=3)
assert approx(out_wide[2]["r"], 85, eps=3)
assert out_wide[3]["r"] == 0  # distance 3, brightness = 0
# Wrapping: cell 7 (distance 1 going backward) should also be partial
assert approx(out_wide[7]["r"], 170, eps=3), "chase peak wraps symmetrically"
print("✓ chase: cell 0 bright at t=0, smooth sub-cell motion, wider size, ring wrap")


# ── 4. COMET ───────────────────────────────────────────────────────────────
# Head at cell 0, tail trails BEHIND (which on a ring means wrapping back)
params = {"primary": RED, "secondary": fx.BLACK, "speed": 1.0, "tail": 4}
out = fx.render_comet(10, 0.0, params, ())
assert out[0] == RED, "comet head at t=0 should be cell 0"
# Tail trails behind: cells 9, 8, 7, 6 should be progressively darker
# d=(0-9) mod 10 = 1 → brightness 1 - 1/4 = 0.75
assert approx(out[9]["r"], 191, eps=3), f"cell 9 (d=1) should be ~75%, got {out[9]['r']}"
assert approx(out[8]["r"], 127, eps=3), f"cell 8 (d=2) should be ~50%"
assert approx(out[7]["r"], 63, eps=3),  f"cell 7 (d=3) should be ~25%"
assert out[6]["r"] == 0,                f"cell 6 (d=4 = tail) → off, got {out[6]['r']}"
# Cells in front of head are off (no leading edge)
assert out[1]["r"] == 0
assert out[5]["r"] == 0
print("✓ comet: head at cell 0, tail trails behind with linear falloff, no leading edge")


# ── 5. RAINBOW ─────────────────────────────────────────────────────────────
params = {"speed": 0.0, "density": 1.0, "saturation": 1.0, "value": 1.0}
out = fx.render_rainbow(6, 0.0, params, ())
assert len(out) == 6
# At density=1, cell 0 = red, cell 2 = green (hue 0.33), cell 4 = blue (hue 0.67)
assert out[0]["r"] > 200 and out[0]["g"] < 30, f"cell 0 should be red, got {out[0]}"
assert out[2]["g"] > 200 and out[2]["r"] < 30, f"cell 2 should be green-ish, got {out[2]}"
assert out[4]["b"] > 200 and out[4]["g"] < 30, f"cell 4 should be blue-ish, got {out[4]}"
# Higher density = more cycles visible
out_dense = fx.render_rainbow(6, 0.0, {"speed":0,"density":2,"saturation":1,"value":1}, ())
assert out_dense[3]["r"] > 200 and out_dense[3]["g"] < 30, "with density=2, cell 3 wraps back to red"
# Saturation 0 = white
out_white = fx.render_rainbow(4, 0.0, {"speed":0,"density":1,"saturation":0,"value":1}, ())
for c in out_white:
    assert c["r"] == 255 and c["g"] == 255 and c["b"] == 255
# Value 0.5 = half brightness
out_dim = fx.render_rainbow(2, 0.0, {"speed":0,"density":0.5,"saturation":1,"value":0.5}, ())
assert approx(out_dim[0]["r"], 127, eps=3), f"value=0.5 at red hue gives r=127, got {out_dim[0]['r']}"
# Speed > 0: hue at t = 1/speed should match hue at t=0 (full cycle)
o1 = fx.render_rainbow(4, 0.0, {"speed":1,"density":1,"saturation":1,"value":1}, ())
o2 = fx.render_rainbow(4, 1.0, {"speed":1,"density":1,"saturation":1,"value":1}, ())
assert o1 == o2, "rainbow should cycle exactly at 1/speed seconds"
print("✓ rainbow: hue placement, density, saturation, value, time cycling")


# ── 6. TWINKLE ─────────────────────────────────────────────────────────────
params = {"primary": RED, "secondary": fx.BLACK, "speed": 5.0, "fade": 0.5}
out = fx.render_twinkle(20, 0.0, params, ())
assert len(out) == 20
# Determinism: same t → same output
assert fx.render_twinkle(20, 1.234, params, ()) == fx.render_twinkle(20, 1.234, params, ())
# Across time, the same cell should change state
o1 = fx.render_twinkle(20, 0.0, params, ())
o2 = fx.render_twinkle(20, 0.3, params, ())
o3 = fx.render_twinkle(20, 2.0, params, ())
# At least some cells should differ between time samples
diffs = sum(1 for a, b in zip(o1, o3) if a != b)
assert diffs > 0, "twinkle should change over time"
# All cells should be either RED-tinted or BLACK (no green from somewhere weird)
for c in out:
    assert c["g"] == 0 and c["b"] == 0, f"unexpected colour in twinkle: {c}"
# With accent, some cells should differ from primary-only run
params_accent = dict(params, accent=GREEN)
out_acc = fx.render_twinkle(20, 0.0, params_accent, ())
saw_green = any(c["g"] > 0 for c in out_acc)
assert saw_green, "twinkle with accent should produce some green cells"
# Edge case: speed=0 means all dark
out_zero = fx.render_twinkle(10, 1.0, dict(params, speed=0), ())
assert all(c == fx.BLACK or c.get("r",0)==0 for c in out_zero), "twinkle speed=0 → all secondary"
print("✓ twinkle: determinism, changes over time, accent two-colour, speed=0 → dark")


# ── 7. GRADIENT ────────────────────────────────────────────────────────────
# 2-stop static gradient: primary at cell 0, secondary at cell L-1
params = {"primary": RED, "secondary": BLUE, "speed": 0.0}
out = fx.render_gradient(5, 0.0, params, ())
assert out[0] == RED, f"cell 0 should be primary, got {out[0]}"
assert out[4] == BLUE, f"last cell should be secondary, got {out[4]}"
# Middle cell is the mid blend
assert approx(out[2]["r"], 127, eps=3), f"middle cell r should be ~127, got {out[2]}"
assert approx(out[2]["b"], 127, eps=3)
# 3-stop with accent
params3 = {"primary": RED, "secondary": BLUE, "accent": GREEN, "speed": 0.0}
out3 = fx.render_gradient(5, 0.0, params3, ())
assert out3[0] == RED
assert out3[2] == GREEN  # midpoint = accent
assert out3[4] == BLUE
# Scrolling: at speed=1, time=0 → t * speed = 0; time=1 → offset wraps to 0 again (full traversal)
o0 = fx.render_gradient(5, 0.0, dict(params, speed=1), ())
o1 = fx.render_gradient(5, 1.0, dict(params, speed=1), ())
assert o0 == o1, "gradient scroll should complete one strip in 1/speed seconds"
# Edge case: strip_length=1 returns just primary
assert fx.render_gradient(1, 0.0, params, ()) == [RED]
print("✓ gradient: 2-stop, 3-stop with accent, midpoints, scroll cycling, len=1")


# ── 8. STROBE ──────────────────────────────────────────────────────────────
params = {"primary": RED, "secondary": fx.BLACK, "speed": 10.0, "duty": 0.5}
# At t=0, phase=0, < duty → primary
out = fx.render_strobe(4, 0.0, params, ())
assert all(c == RED for c in out), "strobe at phase 0 should be primary"
# At t=0.075s (3/4 through a 0.1s cycle), phase=0.75 > 0.5 duty → secondary
out_off = fx.render_strobe(4, 0.075, params, ())
assert all(c == fx.BLACK or c.get("r",0)==0 for c in out_off)
# Duty 0.2: only first 20% of cycle is on
out_tail = fx.render_strobe(4, 0.025, dict(params, duty=0.2), ())  # phase=0.25, > 0.2
assert out_tail[0]["r"] == 0
out_head = fx.render_strobe(4, 0.01, dict(params, duty=0.2), ())   # phase 0.1, < 0.2
assert out_head[0] == RED
# Speed 0 → always on
out_static = fx.render_strobe(4, 999.0, dict(params, speed=0), ())
assert all(c == RED for c in out_static)
print("✓ strobe: on at phase<duty, off at phase>duty, duty respected, speed=0 → always on")


# ── 9. DISPATCH ────────────────────────────────────────────────────────────
# Top-level render() routes correctly
out = fx.render("solid", 4, 0.0, {"primary": RED})
assert len(out) == 4 and all(c == RED for c in out)
try:
    fx.render("nonexistent", 4, 0.0, {})
    assert False, "should have raised"
except ValueError:
    pass
print("✓ dispatch: render() works for known effects, raises for unknown")


# ── 10. REGISTRY ───────────────────────────────────────────────────────────
reg = fx.get_registry()
expected = {"solid","breathe","chase","comet","rainbow","twinkle","gradient","strobe",
            "pulse","scanner","wave","fire","marquee","plasma","colorfade","wipe"}
assert set(reg.keys()) == expected, f"missing effects: {expected - set(reg.keys())}"
# Each entry has the expected shape
for eid, info in reg.items():
    assert "name" in info and "description" in info
    assert "uses_primary" in info
    assert "uses_secondary" in info
    assert "uses_accent" in info
    assert "params" in info
    for pname, pspec in info["params"].items():
        assert "type" in pspec
        assert "default" in pspec
        assert "label" in pspec
# Rainbow uniquely has uses_primary=False
assert reg["rainbow"]["uses_primary"] is False
# Every effect gets the universal brightness param, even solid (which has
# no effect-specific params of its own)
assert reg["solid"]["params"] == {"brightness": fx._BRIGHTNESS_PARAM}
for eid, info in reg.items():
    assert "brightness" in info["params"], f"{eid} missing universal brightness param"
    assert info["params"]["brightness"]["default"] == 1.0
# Defaults factory includes brightness alongside the effect's own defaults
d = fx.defaults_for("chase")
assert d == {"brightness": 1.0, "speed": 1.0, "size": 1}
print("✓ registry: complete, shaped correctly, defaults_for() works")


# ── 11. UNIVERSAL BRIGHTNESS ────────────────────────────────────────────────
# Default (omitted) brightness == 1.0, no-op scale — dispatcher output
# matches the effect's raw output exactly.
out_default = fx.render("solid", 4, 0.0, {"primary": RED})
assert all(c == RED for c in out_default), "omitted brightness must be a no-op"

# brightness=1.0 explicit is likewise a no-op
out_full = fx.render("solid", 4, 0.0, {"primary": RED, "brightness": 1.0})
assert all(c == RED for c in out_full)

# brightness=0.5 scales every channel
out_half = fx.render("solid", 4, 0.0, {"primary": RED, "brightness": 0.5})
assert all(approx(c["r"], 127) and c["g"] == 0 and c["b"] == 0 for c in out_half), out_half

# brightness=0.0 goes fully dark regardless of the underlying effect
out_dark = fx.render("chase", 8, 0.0, {"primary": RED, "brightness": 0.0})
assert all(v == 0 for c in out_dark for v in c.values()), out_dark

# Out-of-range values clamp rather than raising or overshooting
out_over  = fx.render("solid", 2, 0.0, {"primary": RED, "brightness": 5.0})
out_under = fx.render("solid", 2, 0.0, {"primary": RED, "brightness": -3.0})
assert all(c == RED for c in out_over), "brightness > 1 should clamp to 1 (no boost)"
assert all(v == 0 for c in out_under for v in c.values()), "negative brightness should clamp to 0"

# Non-numeric brightness falls back to 1.0 rather than raising
out_bad = fx.render("solid", 2, 0.0, {"primary": RED, "brightness": "oops"})
assert all(c == RED for c in out_bad)

# Stacks independently on top of an effect's own brightness-like knob
# (rainbow's `value`): value dims the HSV generation, brightness scales
# the result again afterwards — same hue, roughly half the peak channel.
out_val_full = fx.render("rainbow", 4, 0.0,
                          {"speed": 0, "density": 1, "saturation": 1, "value": 1.0})
out_val_half_brightness = fx.render("rainbow", 4, 0.0,
                          {"speed": 0, "density": 1, "saturation": 1, "value": 1.0,
                           "brightness": 0.5})
peak_full = max(out_val_full[0].values())
peak_half = max(out_val_half_brightness[0].values())
assert approx(peak_half, peak_full * 0.5, eps=3), (peak_full, peak_half)

# Direct render_* calls bypass the dispatcher entirely — brightness in
# params is simply ignored (by design; see effects.py docstring).
direct = fx.render_solid(4, 0.0, {"primary": RED, "brightness": 0.0}, ())
assert all(c == RED for c in direct), \
    "render_solid() called directly must ignore brightness (dispatcher-only feature)"

# None cells (the "leave untouched" convention) pass through unscaled rather
# than crashing _scale() on a None.
class _NoneEffect:
    """Minimal fake effect that emits a None cell, to exercise render()'s
    None-passthrough path without depending on any real effect doing this
    today."""
    @staticmethod
    def render(strip_length, t, params, color_keys):
        return [None, dict(RED)]
fx.EFFECTS["__test_none__"] = {
    "name": "test", "description": "test", "render": _NoneEffect.render,
    "uses_secondary": False, "uses_accent": False, "params": {},
}
try:
    out_none = fx.render("__test_none__", 2, 0.0, {"brightness": 0.5})
    assert out_none[0] is None
    assert approx(out_none[1]["r"], 127)
finally:
    del fx.EFFECTS["__test_none__"]

print("✓ brightness: universal scale, clamping, stacking, None passthrough, dispatcher-only scope")


# ── 12. CROSS-FIXTURE COHERENCE ────────────────────────────────────────────
# A comet on an 8-cell bar and a 189-cell tube at the same t should both have
# heads at the same FRACTIONAL position around their respective strips.
params = {"primary": RED, "secondary": fx.BLACK, "speed": 1.0, "tail": 5}
bar = fx.render_comet(8, 0.25, params, ())   # head = 0.25 * 8 = 2.0
tube = fx.render_comet(189, 0.25, params, ()) # head = 0.25 * 189 = 47.25
# Bar cell 2 should be the head
assert bar[2] == RED, f"bar head at cell 2, got {bar[2]}"
# Tube cell 47 should be the head (47.25 means ~47 with a tiny offset)
assert tube[47]["r"] > 240, f"tube head near cell 47, got r={tube[47]['r']}"
print("✓ cross-fixture: same effect at same t produces coherent positions across strip lengths")


# ── 13. END-TO-END: effect → cell_strip_to_dmx ─────────────────────────────
# Verify an effect's output can be written through cell_strip's writer
import cell_strip as cs
exa = {
    "id":"exa","name":"EXA","universe":1,"start_address":1,"channels":51,
    "pods":8,"dimmer_channel":1,"first_pod_channel":2,"channels_per_pod":6,
    "pod_color_offsets":{"r":0,"g":1,"b":2,"a":3,"w":4,"uv":5},
}
strip = cs.build_cell_strips(exa)[0]
cells = fx.render("chase", strip["length"], 0.0,
                  {"primary": RED, "secondary": fx.BLACK, "speed": 1.0, "size": 1})
dmx = cs.cell_strip_to_dmx(strip, cells)
# Pod 0 r at (1, 2) should be 255
assert dmx[(1, 2)] == 255, f"chase on EXA: pod 0 r should be 255, got {dmx.get((1,2))}"
# Pod 0 g, b should be 0
assert dmx[(1, 3)] == 0
# Pod 0 'a' offset 3 → channel 5 should also be 0 (RED has no 'a' key, defaulted to 0)
assert dmx[(1, 5)] == 0
print("✓ end-to-end: effect → cell_strip_to_dmx produces correct DMX dict")


print("\nAll 12 effect-test sections passed ✓")
