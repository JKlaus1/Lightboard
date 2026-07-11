"""
Color temperature → per-engine channel recipe translation.

Purpose: the singer warm-white override (and potentially other features
later) needs ONE user-facing control — a Kelvin slider — that renders as
matched white on fixtures with different LED engines (RGBAWUV Blizzard
bars, RGBLAUV Betopper pars, plain RGB, etc).

Approach: rather than deriving channel values from blackbody theory (which
ignores each fixture's actual emitter spectra), we interpolate between the
hand-tuned `warm_white` and `cool_white` recipes already in palette.json.
Those recipes were eyeballed on the real fixtures, so the endpoints are
grounded; the slider blends between them per engine. Outside the anchor
range we extrapolate linearly and clamp 0–255.

Per-engine trim: an optional {engine: {channel: multiplier}} table applied
after interpolation, so two fixture models that still don't quite match
can be nudged without re-tuning the palette anchors.

Engine detection: a fixture's engine is identified by the KEY SET of its
pod_color_offsets — no new fixture fields required.

All functions are pure except the cached palette loader.
"""

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

# Kelvin anchor points the palette recipes are assumed to represent.
WARM_ANCHOR_K = 2700
COOL_ANCHOR_K = 6000

# Slider / API accepted range.
KELVIN_MIN = 2200
KELVIN_MAX = 6500

# Engine id → channel key set. Order matters only for readability.
_ENGINE_KEYSETS = {
    "rgb":     frozenset(("r", "g", "b")),
    "rgbw":    frozenset(("r", "g", "b", "w")),
    "rgbaw":   frozenset(("r", "g", "b", "a", "w")),
    "rgbawuv": frozenset(("r", "g", "b", "a", "w", "uv")),
    "rgblauv": frozenset(("r", "g", "b", "l", "a", "uv")),
}

# Fallback chain when a palette color has no recipe for the exact engine:
# try these donor engines in order and keep only the channels the target
# engine actually has. Insurance for edge cases (e.g. rgbw fixtures);
# the fixtures in real use (rgbawuv, rgblauv) always hit exact recipes.
_FALLBACK_DONORS = ("rgbawuv", "rgblauv", "rgbaw", "rgbw", "rgb")


def engine_for_offsets(pod_color_offsets):
    """Identify a fixture's color engine from its pod_color_offsets keys.
    Returns an engine id string; unknown key sets fall back to 'rgb' if
    r/g/b are present, else None (caller should skip such fixtures)."""
    if not pod_color_offsets:
        return None
    keys = frozenset(pod_color_offsets.keys())
    for eng, ks in _ENGINE_KEYSETS.items():
        if keys == ks:
            return eng
    if {"r", "g", "b"} <= keys:
        log.warning("Unknown color engine key set %s — treating as rgb", sorted(keys))
        return "rgb"
    return None


# ── Palette loading (cached on mtime) ──────────────────────────────────────

_PALETTE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "palette.json")
_palette_lock  = threading.Lock()
_palette_cache = None   # (mtime, data)


def _load_palette():
    """Load palette.json with an mtime-keyed cache. Returns {} on failure
    (callers then fall back to built-in anchor defaults)."""
    global _palette_cache
    try:
        mtime = os.path.getmtime(_PALETTE_PATH)
    except OSError:
        return {}
    with _palette_lock:
        if _palette_cache and _palette_cache[0] == mtime:
            return _palette_cache[1]
        try:
            with open(_PALETTE_PATH, "r") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.error("color_temp: failed to load palette (%s)", e)
            return {}
        _palette_cache = (mtime, data)
        return data


# Built-in anchor recipes used if palette.json is missing or lacks the
# warm_white / cool_white entries. Mirrors the shipped palette defaults.
_BUILTIN_ANCHORS = {
    "warm_white": {
        "rgb":     {"r": 255, "g": 160, "b": 80},
        "rgbawuv": {"r": 40, "g": 15, "b": 0, "a": 140, "w": 255, "uv": 0},
        "rgblauv": {"r": 150, "g": 80, "b": 40, "l": 180, "a": 160, "uv": 0},
    },
    "cool_white": {
        "rgb":     {"r": 230, "g": 235, "b": 255},
        "rgbawuv": {"r": 30, "g": 40, "b": 60, "a": 0, "w": 255, "uv": 0},
        "rgblauv": {"r": 140, "g": 150, "b": 160, "l": 150, "a": 40, "uv": 0},
    },
}


def _anchor_recipe(color_id, engine):
    """Get the palette recipe for `color_id` (warm_white / cool_white) for
    `engine`, falling back through donor engines filtered to the target
    engine's channels, then to built-in defaults."""
    target_keys = _ENGINE_KEYSETS.get(engine, frozenset(("r", "g", "b")))
    recipes = {}
    pal = _load_palette()
    for c in pal.get("colors", []):
        if c.get("id") == color_id:
            recipes = c.get("recipes", {}) or {}
            break
    if engine in recipes:
        return dict(recipes[engine])
    for donor in _FALLBACK_DONORS:
        if donor in recipes:
            return {k: v for k, v in recipes[donor].items() if k in target_keys}
    # Built-in last resort
    builtin = _BUILTIN_ANCHORS.get(color_id, {})
    if engine in builtin:
        return dict(builtin[engine])
    for donor in _FALLBACK_DONORS:
        if donor in builtin:
            return {k: v for k, v in builtin[donor].items() if k in target_keys}
    return {}


def kelvin_recipe(engine, kelvin, trim=None):
    """Return {channel: 0-255} for `engine` at color temperature `kelvin`.

    Linear interpolation between the palette's warm_white (2700K) and
    cool_white (6000K) recipes for that engine; linear extrapolation
    outside the anchors, clamped to 0–255. `trim` is an optional
    {engine: {channel: multiplier}} table applied after interpolation.
    """
    if engine is None:
        return {}
    try:
        k = float(kelvin)
    except (TypeError, ValueError):
        k = WARM_ANCHOR_K
    k = max(KELVIN_MIN, min(KELVIN_MAX, k))

    warm = _anchor_recipe("warm_white", engine)
    cool = _anchor_recipe("cool_white", engine)
    if not warm and not cool:
        return {}

    t = (k - WARM_ANCHOR_K) / float(COOL_ANCHOR_K - WARM_ANCHOR_K)  # may be <0 or >1

    chans = set(warm) | set(cool)
    eng_trim = (trim or {}).get(engine, {}) if isinstance(trim, dict) else {}
    out = {}
    for ch in chans:
        wv = float(warm.get(ch, 0))
        cv = float(cool.get(ch, 0))
        val = wv + (cv - wv) * t
        try:
            mult = float(eng_trim.get(ch, 1.0))
        except (TypeError, ValueError):
            mult = 1.0
        val *= max(0.0, mult)
        out[ch] = int(round(max(0.0, min(255.0, val))))
    return out


def engines_in_fixtures(fixtures):
    """Sorted list of engine ids present across a show's pod fixtures.
    Used by the settings UI to know which trim rows to render."""
    found = set()
    for fx in fixtures or []:
        eng = engine_for_offsets(fx.get("pod_color_offsets")
                                 or {"r": 0, "g": 1, "b": 2, "a": 3, "w": 4, "uv": 5})
        if eng:
            found.add(eng)
    return sorted(found)
