"""
Color temperature → per-engine channel recipe translation.

Purpose: the singer warm-white override (and potentially other features
later) needs ONE user-facing control — a Kelvin slider — that renders as
matched white on fixtures with different LED engines (RGBAWUV Blizzard
bars, RGBLAUV Betopper pars, plain RGB, etc).

Approach (v2): dedicated multi-point Kelvin anchors per engine, piecewise
linearly interpolated. The slider range (2200–6500K) equals the anchor
range, so no extrapolation ever happens. A mid anchor at 4000K keeps the
middle of the range from drifting off-white.

Why NOT the palette warm_white/cool_white recipes (the v1 approach): those
recipes keep the W emitter pinned at 255 across the whole range — tuned to
look good as scene colors, they render far cooler than their nominal
temperature (a "2700K" anchor that reads ~3500-4000K on the EXA), and no
flat trim multiplier can fix a warm-end-only problem. Retuning them was
ruled out because saved scenes reference those palette recipes.

Anchor defaults live below; they can be overridden without a code change
via a "kelvin_anchors" key in palette.json:

    "kelvin_anchors": [
      {"kelvin": 2200, "recipes": {"rgbawuv": {...}, "rgblauv": {...}}},
      {"kelvin": 4000, "recipes": {...}},
      {"kelvin": 6500, "recipes": {...}}
    ]

Override entries merge per (kelvin, engine): an override recipe replaces
the default recipe for that engine at that anchor point; anchors at new
kelvin values are inserted. Per-engine trim multipliers still apply after
interpolation for final fixture-matching nudges.

Engine detection: a fixture's engine is identified by the KEY SET of its
pod_color_offsets — no new fixture fields required.

All functions are pure except the cached palette loader.
"""

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

# Slider / API accepted range. Anchors below must span this range.
KELVIN_MIN = 2200
KELVIN_MAX = 6500

# Engine id → channel key set.
_ENGINE_KEYSETS = {
    "rgb":     frozenset(("r", "g", "b")),
    "rgbw":    frozenset(("r", "g", "b", "w")),
    "rgbaw":   frozenset(("r", "g", "b", "a", "w")),
    "rgbawuv": frozenset(("r", "g", "b", "a", "w", "uv")),
    "rgblauv": frozenset(("r", "g", "b", "l", "a", "uv")),
}

# Fallback chain when an anchor has no recipe for the exact engine: try
# these donor engines in order, keeping only the target engine's channels.
_FALLBACK_DONORS = ("rgbawuv", "rgblauv", "rgbaw", "rgbw", "rgb")

# ── Default Kelvin anchors ─────────────────────────────────────────────────
# First-pass estimates to be eyeballed on the real fixtures and refined via
# palette.json "kelvin_anchors" overrides. Design intent per engine:
#   rgbawuv — 2200K is amber-dominant with W nearly OFF (incandescent-dim
#             look); W only takes over through the mid band and up.
#   rgblauv — no white emitter: warm end rides amber+red, lime fills the
#             green gap through the mid band, blue lifts the cool end.
#   rgb     — blackbody-ish endpoints.
_DEFAULT_ANCHORS = [
    {
        "kelvin": 2200,
        "recipes": {
            "rgb":     {"r": 255, "g": 120, "b": 25},
            "rgbawuv": {"r": 70,  "g": 8,   "b": 0,  "a": 255, "w": 25,  "uv": 0},
            "rgblauv": {"r": 200, "g": 25,  "b": 0,  "l": 70,  "a": 255, "uv": 0},
        },
    },
    {
        "kelvin": 4000,
        "recipes": {
            "rgb":     {"r": 255, "g": 200, "b": 140},
            "rgbawuv": {"r": 30,  "g": 20,  "b": 5,  "a": 90,  "w": 255, "uv": 0},
            "rgblauv": {"r": 150, "g": 90,  "b": 55, "l": 170, "a": 120, "uv": 0},
        },
    },
    {
        "kelvin": 6500,
        "recipes": {
            "rgb":     {"r": 225, "g": 235, "b": 255},
            "rgbawuv": {"r": 25,  "g": 40,  "b": 75, "a": 0,   "w": 255, "uv": 0},
            "rgblauv": {"r": 135, "g": 150, "b": 185, "l": 140, "a": 20, "uv": 0},
        },
    },
]


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
    """Load palette.json with an mtime-keyed cache. Returns {} on failure."""
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


def _merged_anchors():
    """Default anchors merged with palette.json 'kelvin_anchors' overrides,
    sorted by kelvin. Override recipes replace defaults per (kelvin, engine);
    new kelvin points are inserted. Malformed override entries are skipped."""
    anchors = {a["kelvin"]: {e: dict(r) for e, r in a["recipes"].items()}
               for a in _DEFAULT_ANCHORS}
    pal = _load_palette()
    for ov in pal.get("kelvin_anchors", []) or []:
        try:
            k = float(ov["kelvin"])
            recipes = ov.get("recipes", {})
            if not isinstance(recipes, dict):
                continue
        except (KeyError, TypeError, ValueError):
            log.warning("color_temp: skipping malformed kelvin_anchors entry: %r", ov)
            continue
        slot = anchors.setdefault(k, {})
        for eng, recipe in recipes.items():
            if isinstance(recipe, dict):
                slot[eng] = {ch: v for ch, v in recipe.items()}
    return [{"kelvin": k, "recipes": anchors[k]} for k in sorted(anchors)]


def _recipe_at_anchor(anchor, engine):
    """Recipe for `engine` at one anchor point, via donor fallback if the
    exact engine isn't specified there."""
    target_keys = _ENGINE_KEYSETS.get(engine, frozenset(("r", "g", "b")))
    recipes = anchor.get("recipes", {})
    if engine in recipes:
        return recipes[engine]
    for donor in _FALLBACK_DONORS:
        if donor in recipes:
            return {ch: v for ch, v in recipes[donor].items() if ch in target_keys}
    return {}


def kelvin_recipe(engine, kelvin, trim=None):
    """Return {channel: 0-255} for `engine` at color temperature `kelvin`.

    Piecewise-linear interpolation across the merged anchor set (defaults
    spanning 2200-6500K, optionally overridden/extended via palette.json
    'kelvin_anchors'). Kelvin is clamped to the anchor span. `trim` is an
    optional {engine: {channel: multiplier}} table applied afterward.
    """
    if engine is None:
        return {}
    try:
        k = float(kelvin)
    except (TypeError, ValueError):
        k = KELVIN_MIN
    anchors = _merged_anchors()
    if not anchors:
        return {}
    k = max(anchors[0]["kelvin"], min(anchors[-1]["kelvin"], k))

    # Locate bracketing anchor pair
    lo = anchors[0]
    hi = anchors[-1]
    for i in range(len(anchors) - 1):
        if anchors[i]["kelvin"] <= k <= anchors[i + 1]["kelvin"]:
            lo, hi = anchors[i], anchors[i + 1]
            break
    lo_r = _recipe_at_anchor(lo, engine)
    hi_r = _recipe_at_anchor(hi, engine)
    if not lo_r and not hi_r:
        return {}
    span = float(hi["kelvin"] - lo["kelvin"]) or 1.0
    t = (k - lo["kelvin"]) / span

    chans = set(lo_r) | set(hi_r)
    eng_trim = (trim or {}).get(engine, {}) if isinstance(trim, dict) else {}
    out = {}
    for ch in chans:
        lv = float(lo_r.get(ch, 0))
        hv = float(hi_r.get(ch, 0))
        val = lv + (hv - lv) * t
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


def _apply_trim_clamp(recipe, engine, trim):
    """Apply per-engine trim multipliers to a recipe and clamp to 0-255 ints."""
    eng_trim = (trim or {}).get(engine, {}) if isinstance(trim, dict) else {}
    out = {}
    for ch, v in recipe.items():
        try:
            val = float(v)
        except (TypeError, ValueError):
            val = 0.0
        try:
            mult = float(eng_trim.get(ch, 1.0))
        except (TypeError, ValueError):
            mult = 1.0
        out[ch] = int(round(max(0.0, min(255.0, val * max(0.0, mult)))))
    return out


def palette_recipe(color_id, engine, trim=None):
    """{channel: 0-255} for palette color `color_id` rendered on `engine`.

    Uses the palette's hand-tuned per-engine recipe when one exists (this is
    the whole point: the palette recipes were eyeballed per LED engine, so no
    translation layer is needed); falls back through donor engines filtered
    to the target engine's channels otherwise. Per-engine trim multipliers
    apply as in kelvin_recipe. Returns {} when the color id or engine can't
    be resolved — callers fall back to the legacy singer_color values.
    """
    if engine is None or not color_id:
        return {}
    pal = _load_palette()
    for c in pal.get("colors", []):
        if c.get("id") == color_id:
            recipes = c.get("recipes", {}) or {}
            target_keys = _ENGINE_KEYSETS.get(engine, frozenset(("r", "g", "b")))
            if engine in recipes and isinstance(recipes[engine], dict):
                return _apply_trim_clamp(recipes[engine], engine, trim)
            for donor in _FALLBACK_DONORS:
                if donor in recipes and isinstance(recipes[donor], dict):
                    filt = {ch: v for ch, v in recipes[donor].items() if ch in target_keys}
                    return _apply_trim_clamp(filt, engine, trim)
            return {}
    return {}
