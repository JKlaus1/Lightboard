"""Unit tests for the procedural mover-motion generator (mover_gen.py).

Pure math — no engine, no Flask. Run: python3 test_mover_gen.py
"""

import math

import mover_gen as mg


def approx(a, b, tol=1.5):
    return abs(a - b) <= tol


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok:", msg)


def test_unit_shapes():
    print("unit_shape:")
    # Circle at theta 0 -> (1, 0); quarter -> (~0, 1).
    x, y = mg.unit_shape("circle", 0.0)
    check(approx(x, 1.0, 0.001) and approx(y, 0.0, 0.001), "circle @0 = (1,0)")
    x, y = mg.unit_shape("circle", 0.25)
    check(approx(x, 0.0, 0.001) and approx(y, 1.0, 0.001), "circle @0.25 = (0,1)")
    # figure8 at 0 -> (0,0); at 0.25 -> (1, 0)  [sin(pi/2), sin(pi)]
    x, y = mg.unit_shape("figure8", 0.25)
    check(approx(x, 1.0, 0.001) and approx(y, 0.0, 0.001), "figure8 @0.25 = (1,0)")
    # sweep_pan never moves tilt; sweep_tilt never moves pan.
    _, y = mg.unit_shape("sweep_pan", 0.3)
    check(y == 0.0, "sweep_pan keeps tilt at 0")
    x, _ = mg.unit_shape("sweep_tilt", 0.3)
    check(x == 0.0, "sweep_tilt keeps pan at 0")
    # lissajous a=1,b=2,delta=0 reduces to figure8.
    g = {"lissajous_a": 1, "lissajous_b": 2, "lissajous_delta": 0.0}
    for t in (0.1, 0.37, 0.8):
        lx, ly = mg.unit_shape("lissajous", t, g)
        fx, fy = mg.unit_shape("figure8", t)
        check(approx(lx, fx, 0.001) and approx(ly, fy, 0.001),
              f"lissajous(1,2,0) == figure8 @{t}")
    # Unknown shape -> hold center.
    check(mg.unit_shape("nope", 0.5) == (0.0, 0.0), "unknown shape holds center")


def test_new_shapes():
    print("spiral / rose / bounce / spirograph / star / heart / drift:")
    g = {"lissajous_a": 3, "lissajous_b": 2, "lissajous_delta": 0.5}
    for shape in ("spiral", "rose", "bounce", "spirograph", "star", "heart", "drift"):
        for k in range(0, 240):
            t = k / 120.0
            x, y = mg.unit_shape(shape, t, g)
            check_silent(-1.0001 <= x <= 1.0001 and -1.0001 <= y <= 1.0001,
                         f"{shape} in box @t={t:.3f} -> ({x:.2f},{y:.2f})")
    print("  ok: all shapes stay within the unit box")
    sx, sy = mg.unit_shape("spiral", 0.0, g)
    check(abs(sx) < 1e-9 and abs(sy) < 1e-9, "spiral starts at center")
    bx, by = mg.unit_shape("bounce", 0.0, g)
    check(bx == -1.0 and by == -1.0, "bounce starts at a corner")
    # Star hits a vertex (on the unit circle) at a segment boundary.
    vx, vy = mg.unit_shape("star", 0.0, {"lissajous_a": 5, "lissajous_b": 2})
    check(abs((vx * vx + vy * vy) - 1.0) < 1e-6, "star vertex sits on the unit circle")
    # Spirograph with d=0 reduces to a circle.
    cx, cy = mg.unit_shape("spirograph", 0.0, {"lissajous_a": 5, "lissajous_delta": 0.0})
    check(abs(cx - 1.0) < 1e-9, "spirograph d=0 -> circle")


def test_modifiers():
    print("modifiers (dwell / snap / breathe / invert / scatter):")
    base = {"scene_type": "mover_motion", "motion_mode": "generator",
            "generator": {"shape": "sweep_pan", "center_pan": 128, "center_tilt": 128,
                          "size_pan": 100, "size_tilt": 100},
            "fixtures": ["m0"], "phase": {"mode": "manual", "offsets": {"m0": 0.0}}}

    # Dwell: at t=0 both eased and linear sit at the seam, but mid-loop the eased
    # phase lags the linear one (slower start) -> different position.
    g = dict(base, generator=dict(base["generator"], dwell=1.0))
    lin = mg.evaluate_motion_generator(base, 0.1)["m0"]["pan"]
    eased = mg.evaluate_motion_generator(g, 0.1)["m0"]["pan"]
    check(eased != lin, f"dwell warps the phase (eased {eased} != linear {lin})")

    # Snap: with snap_steps=4 the value is constant across a quarter-loop band.
    s = dict(base, generator=dict(base["generator"], snap_steps=4))
    a = mg.evaluate_motion_generator(s, 0.05)["m0"]["pan"]
    b = mg.evaluate_motion_generator(s, 0.20)["m0"]["pan"]   # same step [0,0.25)
    check(a == b, f"snap holds within a step ({a} == {b})")
    c = mg.evaluate_motion_generator(s, 0.30)["m0"]["pan"]   # next step
    check(c != a, "snap advances to the next position")

    # Breathe: size grows then shrinks -> the swept extreme differs over time.
    br = dict(base, generator=dict(base["generator"], breathe_depth=0.5, breathe_rate=1.0))
    # quarter loop = sweep extreme; breathe phase changes the reach.
    p_a = mg.evaluate_motion_generator(br, 0.25)["m0"]["pan"]
    p_b = mg.evaluate_motion_generator(br, 0.25 + 0.5)["m0"]["pan"]
    check(p_a != p_b, f"breathing changes reach over time ({p_a} vs {p_b})")

    # Invert: pan flips around the center.
    inv = dict(base, inverts={"m0": {"pan": True}})
    normal = mg.evaluate_motion_generator(base, 0.25)["m0"]["pan"]   # sweep extreme
    flipped = mg.evaluate_motion_generator(inv, 0.25)["m0"]["pan"]
    check(abs((normal - 128) + (flipped - 128)) <= 1, f"pan invert mirrors about center ({normal}/{flipped})")

    # Scatter: stable, in [0,1), and differs across indices.
    cfg = {"mode": "scatter"}
    s0 = mg.phase_for(0, 4, cfg, "a")
    s1 = mg.phase_for(1, 4, cfg, "b")
    check(0 <= s0 < 1 and 0 <= s1 < 1 and s0 != s1, f"scatter spreads ({s0:.2f},{s1:.2f})")
    check(mg.phase_for(0, 4, cfg, "a") == s0, "scatter is stable (seeded)")


def check_silent(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_phase_even():
    print("phase even distribution:")
    cfg = {"mode": "even", "spread": 1.0}
    n = 4
    got = [mg.phase_for(i, n, cfg, f"m{i}") for i in range(n)]
    check(got == [0.0, 0.25, 0.5, 0.75], f"4 movers evenly = {got}")
    # spread 0.5 bunches them across half the loop.
    cfg2 = {"mode": "even", "spread": 0.5}
    got2 = [mg.phase_for(i, n, cfg2, f"m{i}") for i in range(n)]
    check(got2 == [0.0, 0.125, 0.25, 0.375], f"spread 0.5 = {got2}")
    # n<=1 -> no offset.
    check(mg.phase_for(0, 1, cfg, "solo") == 0.0, "single fixture = phase 0")


def test_phase_manual():
    print("phase manual:")
    cfg = {"mode": "manual", "offsets": {"a": 0.0, "b": 0.3, "c": 1.25}}
    check(mg.phase_for(0, 3, cfg, "a") == 0.0, "manual a = 0.0")
    check(approx(mg.phase_for(1, 3, cfg, "b"), 0.3, 0.0001), "manual b = 0.3")
    # 1.25 wraps to 0.25
    check(approx(mg.phase_for(2, 3, cfg, "c"), 0.25, 0.0001), "manual c wraps to 0.25")
    # missing fixture -> 0
    check(mg.phase_for(0, 3, cfg, "zzz") == 0.0, "manual missing = 0.0")


def test_split16():
    print("16-bit split:")
    check(mg._split16(0.0) == (0, 0), "0 -> (0,0)")
    check(mg._split16(128.0) == (128, 0), "128 -> (128,0)")
    check(mg._split16(128.5) == (128, 128), "128.5 -> (128,128)")
    check(mg._split16(255.0) == (255, 0), "255 -> (255,0)")
    # clamp below/above travel
    check(mg._split16(-50.0) == (0, 0), "negative clamps to (0,0)")
    check(mg._split16(999.0) == (255, 255), "huge clamps to (255,255)")


def test_evaluate_circle_phase():
    print("evaluate full circle with even phase:")
    scene = {
        "scene_type": "mover_motion",
        "motion_mode": "generator",
        "generator": {
            "shape": "circle",
            "center_pan": 128, "center_tilt": 128,
            "size_pan": 100, "size_tilt": 100,
        },
        "fixtures": ["m0", "m1", "m2", "m3"],
        "phase": {"mode": "even", "spread": 1.0},
    }
    # At t_eff = 0, m0 is at phase 0 -> circle(0) = (1,0) -> pan high, tilt center.
    out = mg.evaluate_motion_generator(scene, 0.0)
    check(set(out.keys()) == {"m0", "m1", "m2", "m3"}, "all 4 fixtures present")
    check(set(out["m0"].keys()) == {"pan", "pan_fine", "tilt", "tilt_fine"},
          "roles = pan/pan_fine/tilt/tilt_fine")
    # m0: pan = 128 + 100*1 = 228 ; tilt = 128 + 100*0 = 128
    check(out["m0"]["pan"] == 228 and out["m0"]["tilt"] == 128,
          f"m0 @t0 pan/tilt = {out['m0']['pan']}/{out['m0']['tilt']}")
    # m1 is a quarter-loop ahead -> circle(0.25) = (0,1) -> pan center, tilt high.
    check(out["m1"]["pan"] == 128 and out["m1"]["tilt"] == 228,
          f"m1 @t0 pan/tilt = {out['m1']['pan']}/{out['m1']['tilt']}")
    # m2 half-loop -> (-1,0) -> pan = 128-100 = 28
    check(out["m2"]["pan"] == 28 and out["m2"]["tilt"] == 128,
          f"m2 @t0 pan/tilt = {out['m2']['pan']}/{out['m2']['tilt']}")


def test_evaluate_direction_and_time():
    print("direction + time advance:")
    base = {
        "scene_type": "mover_motion", "motion_mode": "generator",
        "generator": {"shape": "circle", "center_pan": 128, "center_tilt": 128,
                      "size_pan": 100, "size_tilt": 100},
        "fixtures": ["m0"], "phase": {"mode": "even"},
    }
    # Forward quarter cycle -> tilt rises to top.
    fwd = mg.evaluate_motion_generator(base, 0.25)
    check(fwd["m0"]["tilt"] == 228, f"forward @0.25 tilt high = {fwd['m0']['tilt']}")
    # Reverse quarter cycle -> tilt drops to bottom.
    rev_scene = dict(base)
    rev_scene["generator"] = dict(base["generator"], direction=-1)
    rev = mg.evaluate_motion_generator(rev_scene, 0.25)
    check(rev["m0"]["tilt"] == 28, f"reverse @0.25 tilt low = {rev['m0']['tilt']}")


def test_evaluate_empty():
    print("empty / degenerate:")
    check(mg.evaluate_motion_generator({"generator": {}, "fixtures": []}, 0.0) == {},
          "no fixtures -> {}")
    # Missing generator block: tolerated -> defaults (circle, center 128,
    # size 96). At t0 that's pan = 128 + 96 = 224, tilt = 128. Just confirm it
    # produces a complete, in-range frame without raising.
    out = mg.evaluate_motion_generator({"fixtures": ["m0"]}, 0.0)
    check(out["m0"]["pan"] == 224 and out["m0"]["tilt"] == 128,
          "missing generator -> circle defaults")
    check(all(0 <= out["m0"][r] <= 255 for r in out["m0"]),
          "missing generator -> all roles in 0..255")


def test_evaluate_center_offsets():
    print("per-fixture center offsets:")
    scene = {
        "scene_type": "mover_motion", "motion_mode": "generator",
        "generator": {"shape": "circle", "center_pan": 128, "center_tilt": 128,
                      "size_pan": 0, "size_tilt": 0},   # size 0 => sits at center
        "fixtures": ["m0", "m1"],
        "phase": {"mode": "even"},
        "center_offsets": {"m1": {"dp": 40, "dt": -30}},
    }
    out = mg.evaluate_motion_generator(scene, 0.0)
    check(out["m0"]["pan"] == 128 and out["m0"]["tilt"] == 128, "m0 at shared center")
    check(out["m1"]["pan"] == 168 and out["m1"]["tilt"] == 98,
          f"m1 trimmed to {out['m1']['pan']}/{out['m1']['tilt']}")
    # Offset composes with the orbit: m1's circle is centered on its own center.
    # Pin phase to 0 so circle(0) = (1, 0) and the math is exact.
    scene["phase"] = {"mode": "manual", "offsets": {"m0": 0.0, "m1": 0.0}}
    scene["generator"]["size_pan"] = 50
    scene["generator"]["size_tilt"] = 50
    out2 = mg.evaluate_motion_generator(scene, 0.0)  # circle(0) = (1,0)
    check(out2["m1"]["pan"] == 168 + 50 and out2["m1"]["tilt"] == 98,
          "m1 orbits around its own trimmed center")


def main():
    tests = [
        test_unit_shapes, test_new_shapes, test_modifiers, test_phase_even,
        test_phase_manual, test_split16, test_evaluate_circle_phase,
        test_evaluate_direction_and_time, test_evaluate_center_offsets,
        test_evaluate_empty,
    ]
    for t in tests:
        t()
    print("\nALL MOVER-GEN TESTS PASSED")


if __name__ == "__main__":
    main()
