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


def main():
    tests = [
        test_unit_shapes, test_phase_even, test_phase_manual, test_split16,
        test_evaluate_circle_phase, test_evaluate_direction_and_time,
        test_evaluate_empty,
    ]
    for t in tests:
        t()
    print("\nALL MOVER-GEN TESTS PASSED")


if __name__ == "__main__":
    main()
