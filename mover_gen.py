"""Procedural mover-motion generator (Lightboard).

Pure, Flask-free evaluation of continuous mover movement shapes. The engine's
motion player thread calls evaluate_motion_generator() once per output tick for
a 'generator'-mode mover_motion scene and writes the result to the entry's dmx,
exactly where the old step-based loop wrote it — so freeze / preview / presets /
compositing all flow unchanged (they only ever read entry['dmx']).

Scene shape (a generator-mode mover_motion scene)::

    {
      "scene_type":  "mover_motion",
      "motion_mode": "generator",
      "tempo_sync":  true,
      "beat_division": 1.0,
      "generator": {
        "shape": "circle"|"figure8"|"lissajous"|"sweep_pan"|"sweep_tilt",
        "center_pan":  128, "center_tilt": 128,   # 0..255 coarse DMX units
        "size_pan":     96, "size_tilt":    96,    # half-extent, coarse units
        "speed":      0.25,                         # cycles/sec (wall-clock only)
        "lissajous_a": 1, "lissajous_b": 2, "lissajous_delta": 0.0,
        "direction":   1                            # 1 or -1 (reverse)
      },
      "fixtures": ["mover1", "mover2", ...],         # ordered target list
      "phase": {
        "mode":    "even"|"manual",
        "spread":  1.0,                              # even-mode loop fraction
        "offsets": { "mover1": 0.0, "mover2": 0.25 } # manual-mode, cycles 0..1
      }
    }

The evaluator returns ``{fxid: {pan, pan_fine, tilt, tilt_fine}}`` with 8-bit
role values. resolve_step() maps those roles to absolute DMX channels and
silently drops the *_fine roles for fixtures that don't define them, so the same
output drives both 8-bit and 16-bit movers.

Coordinate convention: positions are computed in 0..255 *coarse* DMX units
(center +/- size), then promoted to 16-bit as ``v16 = round(coarse * 256)`` and
split into high byte (coarse role) + low byte (fine role). The fine byte just
adds sub-coarse smoothness for 16-bit movers; the coarse byte alone always
equals what the operator dialled on the 0..255 grid. Out-of-range positions are
clamped to the 0..65535 travel limit (the path clips at the edge of travel,
matching the grid overlay in the editor).
"""

import math

_TWO_PI = 2.0 * math.pi

SHAPES = ("circle", "figure8", "lissajous", "sweep_pan", "sweep_tilt")


def _f(d, key, default):
    """Tolerant float read: bad/missing values fall back to default."""
    try:
        v = d.get(key, default)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def unit_shape(shape, theta, gen=None):
    """Return (x, y), each in [-1, 1], for the unit shape at phase ``theta``
    (in cycles; one full loop per unit). Mirrors the JS unitShape() in the
    editor so the traced preview path matches live output exactly."""
    gen = gen or {}
    a = theta * _TWO_PI
    if shape == "circle":
        return math.cos(a), math.sin(a)
    if shape == "figure8":
        # Lissajous 1:2 — a horizontal figure-eight.
        return math.sin(a), math.sin(2.0 * a)
    if shape == "lissajous":
        fa = _f(gen, "lissajous_a", 1.0) or 1.0
        fb = _f(gen, "lissajous_b", 2.0) or 2.0
        delta = _f(gen, "lissajous_delta", 0.0) * _TWO_PI
        return math.sin(fa * a), math.sin(fb * a + delta)
    if shape == "sweep_pan":
        return math.sin(a), 0.0
    if shape == "sweep_tilt":
        return 0.0, math.sin(a)
    # Unknown shape -> hold at center.
    return 0.0, 0.0


def phase_for(index, n, phase_cfg, fxid):
    """Phase offset (in cycles, 0..1) for fixture ``fxid`` at position
    ``index`` of ``n`` selected fixtures.

    even   -> spread evenly around the loop: (index / n) * spread.
    manual -> use the stored per-fixture offset (default 0).
    """
    phase_cfg = phase_cfg or {}
    if phase_cfg.get("mode") == "manual":
        offsets = phase_cfg.get("offsets") or {}
        try:
            v = offsets.get(fxid, 0.0)
            return float(0.0 if v is None else v) % 1.0
        except (TypeError, ValueError):
            return 0.0
    # even (default)
    if n <= 1:
        return 0.0
    spread = _f(phase_cfg, "spread", 1.0)
    return ((index / float(n)) * spread) % 1.0


def _split16(coarse_value):
    """coarse_value: float in 0..255 coarse units. Promote to 16-bit, clamp to
    the travel limit, and split into (coarse_byte, fine_byte)."""
    v16 = int(round(coarse_value * 256.0))
    if v16 < 0:
        v16 = 0
    elif v16 > 65535:
        v16 = 65535
    return (v16 >> 8) & 0xFF, v16 & 0xFF


def evaluate_motion_generator(scene, t_eff):
    """Evaluate a generator-mode mover_motion ``scene`` at musical/clock time
    ``t_eff`` (in cycles). Returns ``{fxid: {role: int}}`` for pan/pan_fine/
    tilt/tilt_fine, ready to hand to resolve_step(scene_type='mover_motion')."""
    gen = scene.get("generator") or {}
    fixtures = scene.get("fixtures") or []
    phase_cfg = scene.get("phase") or {}
    center_offsets = scene.get("center_offsets") or {}

    shape = gen.get("shape", "circle")
    direction = -1.0 if str(gen.get("direction", 1)) in ("-1", "-1.0") else 1.0
    center_pan = _f(gen, "center_pan", 128.0)
    center_tilt = _f(gen, "center_tilt", 128.0)
    size_pan = _f(gen, "size_pan", 96.0)
    size_tilt = _f(gen, "size_tilt", 96.0)

    n = len(fixtures)
    out = {}
    for i, fxid in enumerate(fixtures):
        # Per-fixture center trim: each mover orbits its OWN center (group
        # center + this fixture's offset), so movers hung apart can converge.
        off = center_offsets.get(fxid) or {}
        cpan = center_pan + _f(off, "dp", 0.0)
        ctilt = center_tilt + _f(off, "dt", 0.0)
        ph = phase_for(i, n, phase_cfg, fxid)
        theta = direction * t_eff + ph
        x, y = unit_shape(shape, theta, gen)
        pan_coarse, pan_fine = _split16(cpan + size_pan * x)
        tilt_coarse, tilt_fine = _split16(ctilt + size_tilt * y)
        out[fxid] = {
            "pan": pan_coarse,
            "pan_fine": pan_fine,
            "tilt": tilt_coarse,
            "tilt_fine": tilt_fine,
        }
    return out
