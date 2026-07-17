"""
Effects engine — Phase 2B.

Eight stock effects as pure, stateless render functions over cell strips
(see cell_strip.py for the strip abstraction). Each effect has the
signature:

    render_<name>(strip_length, t, params, color_keys) -> list[dict]

  strip_length : number of cells in the target strip
  t            : seconds since the effect scene started (monotonic float)
  params       : per-effect parameter dict (see EFFECTS registry for schema)
  color_keys   : tuple of color keys the target strip uses, e.g. ("r","g","b","w").
                 Most effects ignore this and just emit whatever keys are in
                 their input color dicts; the cell-strip writer from 2A
                 defaults missing keys to 0. Rainbow is the exception — it
                 generates RGB from scratch.

Returns a list of color dicts (one per cell). None entries are allowed and
mean "leave this cell untouched" so a lower-priority layer can fill it in;
for Phase 2B all effects emit a colour for every cell so the effect fully
owns its participating fixtures.

The EFFECTS registry below describes every effect's parameter schema,
default values, and which colour slots it consumes. The editor UI reads
this to render appropriate controls.

Every effect also exposes a universal `brightness` param (0..1, default
1.0) that is NOT part of any individual effect's own params dict — it's
injected by get_registry()/defaults_for() and applied by the top-level
render() dispatcher as a final post-scale over whatever the effect
produces. Calling a render_<n>() function directly bypasses it.
"""

import math


# ── Colour helpers ──────────────────────────────────────────────────────────

BLACK = {"r": 0, "g": 0, "b": 0, "w": 0, "a": 0, "uv": 0}


def _scale(color, k):
    """Multiply every channel of a colour dict by scalar k, clamped 0..255.
    Works uniformly across RGB / RGBW / RGBAWUV — scaling a warm-white
    colour dims it correctly, including the w channel."""
    if k <= 0:
        return {key: 0 for key in color}
    if k >= 1:
        return dict(color)
    return {key: int(max(0, min(255, val * k))) for key, val in color.items()}


def _lerp(a, b, t):
    """Blend two colour dicts: returns a at t=0, b at t=1. Operates on the
    union of keys so RGB + RGBW mixes don't drop channels (RGB-only colour
    contributes 0 for its missing w).

    Snaps to a pure dict(a) / dict(b) at the boundaries so callers get
    back the exact input shape (no zero-padded extra keys) when no blend
    is actually happening."""
    if t <= 0.0:
        return dict(a)
    if t >= 1.0:
        return dict(b)
    keys = set(a.keys()) | set(b.keys())
    return {k: int(max(0, min(255, a.get(k, 0) * (1 - t) + b.get(k, 0) * t)))
            for k in keys}


def _hsv_to_rgb(h, s, v):
    """h in [0,1) (wraps), s and v in [0,1]. Returns an {r,g,b} dict 0..255."""
    if s <= 0:
        n = int(max(0, min(255, v * 255)))
        return {"r": n, "g": n, "b": n}
    h6 = (h % 1.0) * 6
    i  = int(h6)
    f  = h6 - i
    p  = v * (1 - s)
    q  = v * (1 - f * s)
    tt = v * (1 - (1 - f) * s)
    if   i == 0: r, g, b = v, tt, p
    elif i == 1: r, g, b = q, v, p
    elif i == 2: r, g, b = p, v, tt
    elif i == 3: r, g, b = p, q, v
    elif i == 4: r, g, b = tt, p, v
    else:        r, g, b = v, p, q
    return {"r": int(r * 255), "g": int(g * 255), "b": int(b * 255)}


def _cell_hash(c):
    """Deterministic 0..1 offset for cell index c. Used by twinkle so each
    cell has a stable per-cell phase that doesn't change between ticks."""
    # Knuth multiplicative hash, mod 2^32
    return ((c * 2654435761) & 0xFFFFFFFF) / 0xFFFFFFFF


# ── Defaults shared across effects ──────────────────────────────────────────

_DEFAULT_PRIMARY   = {"r": 255, "g": 255, "b": 255}
_DEFAULT_SECONDARY = dict(BLACK)

# Universal brightness control — every effect gets this in addition to its
# own params. It's applied as a final post-scale in render() below (NOT
# inside any individual render_* function), so it works uniformly across
# every effect — including ones like rainbow/plasma that already have their
# own "value" knob. Brightness stacks independently on top of those; it
# doesn't replace them.
_BRIGHTNESS_PARAM = {"type": "float", "min": 0.0, "max": 1.0, "step": 0.01,
                     "default": 1.0, "label": "Brightness"}


# ── Effects ─────────────────────────────────────────────────────────────────

def render_solid(strip_length, t, params, color_keys):
    """Static fill with primary."""
    primary = params.get("primary", _DEFAULT_PRIMARY)
    return [dict(primary) for _ in range(strip_length)]


def render_breathe(strip_length, t, params, color_keys):
    """Sinusoidal brightness modulation on primary. Speed in Hz; `floor`
    sets the minimum brightness (so the strip never fully dims if floor>0).
    Starts at the floor (dark) so cycles look like a natural inhale."""
    primary = params.get("primary", _DEFAULT_PRIMARY)
    speed   = float(params.get("speed", 0.5))
    floor   = max(0.0, min(0.95, float(params.get("floor", 0.0))))

    # 0.5 - 0.5*cos gives a 0..1 wave starting at 0 at t=0.
    phase = t * speed * 2 * math.pi
    k     = floor + (1.0 - floor) * (0.5 - 0.5 * math.cos(phase))
    cell  = _scale(primary, k)
    return [dict(cell) for _ in range(strip_length)]


def render_chase(strip_length, t, params, color_keys):
    """Symmetric triangular peak walks across the strip.

    speed : full strip traversals per second (auto-scales to strip length).
    size  : half-width of the peak in cells. size=1 gives a smooth single-
            cell chase; larger sizes give a wider torch-like sweep that
            falls off linearly on both sides.
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    speed     = float(params.get("speed", 1.0))
    size      = max(1, int(params.get("size", 1)))

    if strip_length <= 0:
        return []

    head = (t * speed * strip_length) % strip_length
    half = strip_length / 2
    out  = []
    for c in range(strip_length):
        # Signed ring distance from cell c to head, in (-L/2, L/2]
        d = (head - c) % strip_length
        if d > half:
            d -= strip_length
        brightness = max(0.0, 1.0 - abs(d) / size)
        if brightness <= 0.0:
            out.append(dict(secondary))
        elif brightness >= 1.0:
            out.append(dict(primary))
        else:
            out.append(_lerp(secondary, primary, brightness))
    return out


def render_comet(strip_length, t, params, color_keys):
    """Bright head with a fading tail trailing behind it (no leading edge).

    speed : full strip traversals per second.
    tail  : tail length in cells; brightness falls linearly from 1.0 at
            the head to 0 at distance `tail` behind it.
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    speed     = float(params.get("speed", 1.0))
    tail      = max(1, int(params.get("tail", 5)))

    if strip_length <= 0:
        return []

    head = (t * speed * strip_length) % strip_length
    out  = []
    for c in range(strip_length):
        # Distance behind the head on the ring (always 0..L-1)
        d = (head - c) % strip_length
        if d <= tail:
            out.append(_lerp(primary, secondary, d / tail))
        else:
            out.append(dict(secondary))
    return out


def render_rainbow(strip_length, t, params, color_keys):
    """Hue cycling across cells. Ignores primary/secondary — colour comes
    from the HSV wheel.

    speed      : hue rotations per second (the wheel slides over time).
    density    : full hue rotations visible across one strip at any moment.
                 density=1 → strip shows exactly one rainbow; density=3 →
                 three compressed rainbows.
    saturation : 0..1 (0 = white, 1 = vivid).
    value      : 0..1 (overall brightness).
    """
    speed      = float(params.get("speed", 0.3))
    density    = float(params.get("density", 1.0))
    saturation = max(0.0, min(1.0, float(params.get("saturation", 1.0))))
    value      = max(0.0, min(1.0, float(params.get("value", 1.0))))

    if strip_length <= 0:
        return []

    base = t * speed
    return [
        _hsv_to_rgb((base + c / strip_length * density) % 1.0, saturation, value)
        for c in range(strip_length)
    ]


def render_twinkle(strip_length, t, params, color_keys):
    """Random-feeling per-cell sparkles, deterministic via per-cell hash.

    Each cell twinkles with period = strip_length / speed seconds, fading
    from full brightness to 0 over `fade` seconds, then dark until its
    next cycle. If `accent` is set, cells with odd hash flash accent
    instead of primary, giving a two-colour sparkle.

    speed : total twinkles per second across the whole strip.
    fade  : fade-out duration of each twinkle in seconds.
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    accent    = params.get("accent")
    speed     = float(params.get("speed", 5.0))
    fade      = max(0.05, float(params.get("fade", 0.5)))

    if strip_length <= 0:
        return []
    if speed <= 0:
        return [dict(secondary) for _ in range(strip_length)]

    period = strip_length / speed
    out    = []
    for c in range(strip_length):
        offset     = _cell_hash(c)
        phase      = (t / period + offset) % 1.0
        time_since = phase * period
        if time_since < fade:
            brightness = 1.0 - (time_since / fade)
            base = accent if (accent is not None and (c * 2654435761) & 1) else primary
            # Crossfade from secondary (off) up to scaled colour
            cell = _lerp(secondary, _scale(base, brightness), brightness)
            out.append(cell)
        else:
            out.append(dict(secondary))
    return out


def render_gradient(strip_length, t, params, color_keys):
    """Smooth gradient across the strip. 2 stops by default; if `accent`
    is provided, becomes a 3-stop gradient primary → accent → secondary.

    speed = 0 → static: cell 0 is exactly primary, cell L-1 is exactly
    secondary, accent (if set) sits at the midpoint.
    speed ≠ 0 → scrolling: the gradient pattern wraps cyclically across
    the strip and shifts by `speed` strip-lengths per second.
    """
    primary   = params.get("primary",   {"r": 255, "g": 0,   "b": 0})
    secondary = params.get("secondary", {"r": 0,   "g": 0,   "b": 255})
    accent    = params.get("accent")
    speed     = float(params.get("speed", 0.0))

    if strip_length <= 0:
        return []
    if strip_length == 1:
        return [dict(primary)]

    if speed == 0.0:
        # Static: gradient endpoints land exactly on cells 0 and L-1
        denom = strip_length - 1
        positions = [c / denom for c in range(strip_length)]
    else:
        # Scrolling: positions wrap cleanly so the pattern is continuous
        offset = (t * speed) % 1.0
        positions = [(c / strip_length + offset) % 1.0 for c in range(strip_length)]

    out = []
    for pos in positions:
        if accent is None:
            out.append(_lerp(primary, secondary, pos))
        elif pos < 0.5:
            out.append(_lerp(primary, accent, pos * 2))
        else:
            out.append(_lerp(accent, secondary, (pos - 0.5) * 2))
    return out


def render_strobe(strip_length, t, params, color_keys):
    """All cells flash between primary (on) and secondary (off).

    speed : flashes per second (Hz).
    duty  : fraction of each cycle that's "on" (0.05..0.95).
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    speed     = float(params.get("speed", 10.0))
    duty      = max(0.05, min(0.95, float(params.get("duty", 0.5))))

    if strip_length <= 0:
        return []
    if speed <= 0:
        return [dict(primary) for _ in range(strip_length)]

    phase = (t * speed) % 1.0
    color = primary if phase < duty else secondary
    return [dict(color) for _ in range(strip_length)]


def render_pulse(strip_length, t, params, color_keys):
    """Whole-strip flash with a sharp attack and a decaying fall — the
    musical cousin of breathe/strobe. Unlike breathe's symmetric cosine,
    pulse snaps on fast then falls away, so under tempo_sync (speed in
    cycles/beat) it reads as a punchy hit on each beat.

    speed  : pulses per second (Hz) when free-running; cycles per beat
             window when tempo_sync is on.
    attack : fraction of each cycle spent rising 0→1 (small = snappier).
    curve  : decay sharpness; higher falls off faster after the peak.
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    speed     = float(params.get("speed", 1.0))
    attack    = max(0.01, min(0.5, float(params.get("attack", 0.08))))
    curve     = max(1.0, float(params.get("curve", 3.0)))

    if strip_length <= 0:
        return []
    if speed <= 0:
        return [dict(primary) for _ in range(strip_length)]

    phase = (t * speed) % 1.0
    if phase < attack:
        b = phase / attack                      # rise 0→1
    else:
        x = (phase - attack) / (1.0 - attack)   # 0→1 across the decay
        b = (1.0 - x) ** curve                  # fall 1→0
    cell = _lerp(secondary, primary, b)
    return [dict(cell) for _ in range(strip_length)]


def render_scanner(strip_length, t, params, color_keys):
    """Larson / Cylon eye: a bright head sweeps to one end, bounces, and
    sweeps back, dragging a fading tail behind it in the direction of
    travel. Like comet but reflecting off the ends instead of wrapping.

    speed : full back-and-forth sweeps per second.
    tail  : tail length in cells behind the head.
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    speed     = float(params.get("speed", 1.0))
    tail      = max(1, int(params.get("tail", 6)))

    if strip_length <= 0:
        return []
    if strip_length == 1:
        return [dict(primary)]

    span  = strip_length - 1
    phase = (t * speed) % 1.0
    if phase < 0.5:
        head      = (phase * 2.0) * span
        direction = 1
    else:
        head      = (1.0 - (phase - 0.5) * 2.0) * span
        direction = -1

    out = []
    for c in range(strip_length):
        rel = (c - head) * direction   # >0 ahead of head, <0 behind (the tail)
        if rel >= 0:
            b = max(0.0, 1.0 - rel)            # crisp leading edge (~1 cell)
        else:
            b = max(0.0, 1.0 - (-rel) / tail)  # tail fades over `tail` cells
        out.append(_lerp(secondary, primary, b))
    return out


def render_wave(strip_length, t, params, color_keys):
    """A travelling brightness wave across the strip — a spatial sibling of
    breathe. Cells crest and trough in a moving sine, fading between
    secondary (trough) and primary (crest).

    speed   : wave travel speed in strip-lengths/sec (negative reverses).
    density : number of full waves visible across the strip at once.
    floor   : minimum brightness so troughs never fully black out.
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    speed     = float(params.get("speed", 0.4))
    density   = max(0.1, float(params.get("density", 1.0)))
    floor     = max(0.0, min(0.95, float(params.get("floor", 0.0))))

    if strip_length <= 0:
        return []

    out = []
    for c in range(strip_length):
        ph = c / strip_length * density - t * speed
        w  = 0.5 + 0.5 * math.sin(ph * 2 * math.pi)   # 0..1
        k  = floor + (1.0 - floor) * w
        out.append(_lerp(secondary, primary, k))
    return out


def render_fire(strip_length, t, params, color_keys):
    """Per-cell flame flicker. Each cell wanders between an ember colour
    (secondary) and a flame colour (primary) via layered sine "noise"
    keyed off a stable per-cell hash, with brightness tracking the heat so
    cool cells dim to embers rather than just changing hue.

    Defaults to warm flame/ember colours; override the primary (flame) and
    secondary (ember) slots in the editor for blue fire, etc.

    speed : flicker rate.
    floor : minimum ember heat (keeps the base glowing).
    """
    primary   = params.get("primary",   {"r": 255, "g": 110, "b": 10})
    secondary = params.get("secondary", {"r": 60,  "g": 8,   "b": 0})
    speed     = float(params.get("speed", 3.0))
    floor     = max(0.0, min(0.9, float(params.get("floor", 0.05))))

    if strip_length <= 0:
        return []

    out = []
    for c in range(strip_length):
        h = _cell_hash(c)
        n = (math.sin(t * speed         + h * 6.2832) * 0.5
             + math.sin(t * speed * 2.3 + h * 12.9)   * 0.3
             + math.sin(t * speed * 0.7 + h * 3.1)    * 0.2)   # ~[-1,1]
        k = max(0.0, min(1.0, 0.5 + 0.5 * n))
        k = floor + (1.0 - floor) * k
        base = _lerp(secondary, primary, k)
        out.append(_scale(base, 0.4 + 0.6 * k))
    return out


def render_marquee(strip_length, t, params, color_keys):
    """Theatre-chase marquee: every `spacing`-th cell (a group `width` wide)
    is lit with primary over secondary, and the whole pattern marches one
    cell per step. Steps are discrete, like switching bulbs, so it reads as
    a classic chasing-light border.

    speed   : steps per second (steps per beat under tempo_sync).
    spacing : cells between the start of each lit group.
    width   : how many cells are lit in each group.
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    speed     = float(params.get("speed", 4.0))
    spacing   = max(2, int(params.get("spacing", 3)))
    width     = max(1, min(spacing - 1, int(params.get("width", 1))))

    if strip_length <= 0:
        return []

    step = int(t * speed) if speed > 0 else 0
    out  = []
    for c in range(strip_length):
        on = (c - step) % spacing < width
        out.append(dict(primary) if on else dict(secondary))
    return out


def render_plasma(strip_length, t, params, color_keys):
    """Demoscene-style plasma: several sine waves at different spatial
    frequencies and drift rates sum into an organic, flowing hue field.
    Ignores the colour slots — colour comes from the HSV wheel.

    speed      : evolution rate over time (rotations per beat under sync).
    scale      : spatial frequency; higher packs more colour bands in.
    saturation : 0..1 (0 = greyscale, 1 = vivid).
    value      : 0..1 (overall brightness).
    """
    speed      = float(params.get("speed", 0.3))
    scale      = max(0.1, float(params.get("scale", 1.0)))
    saturation = max(0.0, min(1.0, float(params.get("saturation", 1.0))))
    value      = max(0.0, min(1.0, float(params.get("value", 1.0))))

    if strip_length <= 0:
        return []

    out = []
    for c in range(strip_length):
        px = c / strip_length
        a  = math.sin(px * scale * 3.0 * math.pi + t * speed * 1.7)
        b  = math.sin(px * scale * 5.3 * math.pi - t * speed * 1.1)
        d  = math.sin(px * scale * 1.3 * math.pi + t * speed * 0.9)
        val = (a + b + d) / 3.0                 # ~[-1,1]
        hue = (0.5 + 0.5 * val) % 1.0
        out.append(_hsv_to_rgb(hue, saturation, value))
    return out


def render_colorfade(strip_length, t, params, color_keys):
    """Whole strip is one uniform colour that crossfades smoothly through
    the colour slots and loops. With just primary + secondary it ping-pongs
    between the two; add an accent and it cycles primary → secondary →
    accent → primary. Unlike rainbow/plasma it stays on YOUR colours.

    speed : full loops through the palette per second (loops/beat under sync).
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    accent    = params.get("accent")
    speed     = float(params.get("speed", 0.2))

    if strip_length <= 0:
        return []

    stops = [primary, secondary] + ([accent] if accent is not None else [])
    n = len(stops)
    if n == 1:
        return [dict(primary) for _ in range(strip_length)]

    pos = (t * speed) % 1.0
    seg = pos * n
    i   = int(seg) % n
    f   = seg - int(seg)
    cell = _lerp(stops[i], stops[(i + 1) % n], f)
    return [dict(cell) for _ in range(strip_length)]


def render_wipe(strip_length, t, params, color_keys):
    """A fill front sweeps across the strip laying down primary over
    secondary, then drains back — a continuous fill-and-release that reads
    as a build. Mirror it on a second tower (reverse output) for a
    converging fill.

    speed    : fill-and-drain cycles per second (per beat under tempo_sync).
    softness : feather width of the fill edge in cells (0 = hard edge).
    """
    primary   = params.get("primary",   _DEFAULT_PRIMARY)
    secondary = params.get("secondary", _DEFAULT_SECONDARY)
    speed     = float(params.get("speed", 0.5))
    softness  = max(0.0, float(params.get("softness", 0.0)))

    if strip_length <= 0:
        return []

    phase = (t * speed) % 1.0
    frac  = phase * 2.0 if phase < 0.5 else (1.0 - phase) * 2.0   # fill 0→1→0
    front = frac * strip_length

    out = []
    for c in range(strip_length):
        if softness <= 0:
            b = 1.0 if c < front else 0.0
        else:
            b = max(0.0, min(1.0, (front - c) / softness))
        out.append(_lerp(secondary, primary, b))
    return out


# ── Registry ────────────────────────────────────────────────────────────────
#
# Single source of truth for what effects exist and what knobs they expose.
# The future editor (2D) reads this to build the parameter UI. Effects
# omit "uses_primary" when they use primary (default True); rainbow sets
# it False because it ignores colour-slot input entirely.

EFFECTS = {
    "solid": {
        "name":        "Solid",
        "description": "Static colour fill",
        "render":      render_solid,
        "uses_secondary": False,
        "uses_accent":    False,
        "params":         {},
    },
    "breathe": {
        "name":        "Breathe",
        "description": "Sinusoidal brightness fade on primary colour",
        "render":      render_breathe,
        "uses_secondary": False,
        "uses_accent":    False,
        "params": {
            "speed": {"type":"float","min":0.05,"max":4.0,"step":0.05,"default":0.5,
                      "unit":"Hz","label":"Speed"},
            "floor": {"type":"float","min":0.0,"max":0.9,"step":0.05,"default":0.0,
                      "label":"Min brightness"},
        },
    },
    "chase": {
        "name":        "Chase",
        "description": "Bright peak walks across the strip",
        "render":      render_chase,
        "uses_secondary": True,
        "uses_accent":    False,
        "params": {
            "speed": {"type":"float","min":0.1,"max":10.0,"step":0.1,"default":1.0,
                      "unit":"strips/s","label":"Speed"},
            "size":  {"type":"int","min":1,"max":32,"step":1,"default":1,
                      "unit":"cells","label":"Peak width"},
        },
    },
    "comet": {
        "name":        "Comet",
        "description": "Bright head with a fading tail",
        "render":      render_comet,
        "uses_secondary": True,
        "uses_accent":    False,
        "params": {
            "speed": {"type":"float","min":0.1,"max":10.0,"step":0.1,"default":1.0,
                      "unit":"strips/s","label":"Speed"},
            "tail":  {"type":"int","min":1,"max":64,"step":1,"default":5,
                      "unit":"cells","label":"Tail length"},
        },
    },
    "rainbow": {
        "name":        "Rainbow",
        "description": "Hue cycle across the strip (ignores colour slots)",
        "render":      render_rainbow,
        "uses_primary":   False,
        "uses_secondary": False,
        "uses_accent":    False,
        "params": {
            "speed":      {"type":"float","min":0.0,"max":4.0,"step":0.05,"default":0.3,
                           "unit":"Hz","label":"Cycle speed"},
            "density":    {"type":"float","min":0.1,"max":8.0,"step":0.1,"default":1.0,
                           "unit":"cycles","label":"Cycles per strip"},
            "saturation": {"type":"float","min":0.0,"max":1.0,"step":0.05,"default":1.0,
                           "label":"Saturation"},
            "value":      {"type":"float","min":0.1,"max":1.0,"step":0.05,"default":1.0,
                           "label":"Brightness"},
        },
    },
    "twinkle": {
        "name":        "Twinkle",
        "description": "Random-feeling sparkles that fade out",
        "render":      render_twinkle,
        "uses_secondary": True,
        "uses_accent":    True,
        "params": {
            "speed": {"type":"float","min":0.5,"max":50.0,"step":0.5,"default":5.0,
                      "unit":"twinkles/s","label":"Density"},
            "fade":  {"type":"float","min":0.05,"max":3.0,"step":0.05,"default":0.5,
                      "unit":"sec","label":"Fade time"},
        },
    },
    "gradient": {
        "name":        "Gradient",
        "description": "2- or 3-stop colour gradient, optionally scrolling",
        "render":      render_gradient,
        "uses_secondary": True,
        "uses_accent":    True,
        "params": {
            "speed": {"type":"float","min":-4.0,"max":4.0,"step":0.05,"default":0.0,
                      "unit":"strips/s","label":"Scroll speed"},
        },
    },
    "strobe": {
        "name":        "Strobe",
        "description": "All cells flash on/off",
        "render":      render_strobe,
        "uses_secondary": True,
        "uses_accent":    False,
        "params": {
            "speed": {"type":"float","min":0.5,"max":30.0,"step":0.5,"default":10.0,
                      "unit":"Hz","label":"Rate"},
            "duty":  {"type":"float","min":0.05,"max":0.95,"step":0.05,"default":0.5,
                      "label":"On fraction"},
        },
    },
    "pulse": {
        "name":        "Pulse",
        "description": "Sharp flash with a decaying fall — beat-lock for a punchy hit per beat",
        "render":      render_pulse,
        "uses_secondary": True,
        "uses_accent":    False,
        "params": {
            "speed":  {"type":"float","min":0.1,"max":12.0,"step":0.1,"default":1.0,
                       "unit":"Hz","label":"Rate"},
            "attack": {"type":"float","min":0.01,"max":0.5,"step":0.01,"default":0.08,
                       "label":"Attack"},
            "curve":  {"type":"float","min":1.0,"max":6.0,"step":0.5,"default":3.0,
                       "label":"Decay sharpness"},
        },
    },
    "scanner": {
        "name":        "Scanner",
        "description": "Larson/Cylon eye sweeping back and forth with a trailing tail",
        "render":      render_scanner,
        "uses_secondary": True,
        "uses_accent":    False,
        "params": {
            "speed": {"type":"float","min":0.1,"max":8.0,"step":0.1,"default":1.0,
                      "unit":"sweeps/s","label":"Speed"},
            "tail":  {"type":"int","min":1,"max":48,"step":1,"default":6,
                      "unit":"cells","label":"Tail length"},
        },
    },
    "wave": {
        "name":        "Wave",
        "description": "Travelling brightness wave across the strip (spatial breathe)",
        "render":      render_wave,
        "uses_secondary": True,
        "uses_accent":    False,
        "params": {
            "speed":   {"type":"float","min":-4.0,"max":4.0,"step":0.05,"default":0.4,
                        "unit":"strips/s","label":"Speed"},
            "density": {"type":"float","min":0.2,"max":6.0,"step":0.1,"default":1.0,
                        "unit":"waves","label":"Waves per strip"},
            "floor":   {"type":"float","min":0.0,"max":0.9,"step":0.05,"default":0.0,
                        "label":"Min brightness"},
        },
    },
    "fire": {
        "name":        "Fire",
        "description": "Warm per-cell flame flicker (override slots for other colours)",
        "render":      render_fire,
        "uses_secondary": True,
        "uses_accent":    False,
        "params": {
            "speed": {"type":"float","min":0.2,"max":8.0,"step":0.1,"default":3.0,
                      "unit":"","label":"Flicker speed"},
            "floor": {"type":"float","min":0.0,"max":0.6,"step":0.05,"default":0.05,
                      "label":"Min ember"},
        },
    },
    "marquee": {
        "name":        "Marquee",
        "description": "Theatre-chase: lit cells march one step at a time",
        "render":      render_marquee,
        "uses_secondary": True,
        "uses_accent":    False,
        "params": {
            "speed":   {"type":"float","min":0.5,"max":30.0,"step":0.5,"default":4.0,
                        "unit":"steps/s","label":"Speed"},
            "spacing": {"type":"int","min":2,"max":12,"step":1,"default":3,
                        "unit":"cells","label":"Spacing"},
            "width":   {"type":"int","min":1,"max":6,"step":1,"default":1,
                        "unit":"cells","label":"Lit width"},
        },
    },
    "plasma": {
        "name":        "Plasma",
        "description": "Flowing organic hue field (ignores colour slots)",
        "render":      render_plasma,
        "uses_primary":   False,
        "uses_secondary": False,
        "uses_accent":    False,
        "params": {
            "speed":      {"type":"float","min":0.0,"max":4.0,"step":0.05,"default":0.3,
                           "unit":"Hz","label":"Evolve speed"},
            "scale":      {"type":"float","min":0.1,"max":4.0,"step":0.1,"default":1.0,
                           "label":"Spatial scale"},
            "saturation": {"type":"float","min":0.0,"max":1.0,"step":0.05,"default":1.0,
                           "label":"Saturation"},
            "value":      {"type":"float","min":0.1,"max":1.0,"step":0.05,"default":1.0,
                           "label":"Brightness"},
        },
    },
    "colorfade": {
        "name":        "Color Fade",
        "description": "Uniform strip crossfades through the colour slots and loops",
        "render":      render_colorfade,
        "uses_secondary": True,
        "uses_accent":    True,
        "params": {
            "speed": {"type":"float","min":0.01,"max":2.0,"step":0.01,"default":0.2,
                      "unit":"loops/s","label":"Speed"},
        },
    },
    "wipe": {
        "name":        "Wipe",
        "description": "Fill front sweeps across and drains back — good for builds",
        "render":      render_wipe,
        "uses_secondary": True,
        "uses_accent":    False,
        "params": {
            "speed":    {"type":"float","min":0.05,"max":8.0,"step":0.05,"default":0.5,
                         "unit":"fills/s","label":"Speed"},
            "softness": {"type":"float","min":0.0,"max":16.0,"step":0.5,"default":0.0,
                         "unit":"cells","label":"Edge softness"},
        },
    },
}


# ── Top-level dispatch ──────────────────────────────────────────────────────

def render(effect_id, strip_length, t, params, color_keys=("r","g","b","w")):
    """Dispatch by effect_id. Raises ValueError for unknown effects.

    Applies the universal `brightness` param (0..1, default 1.0) as a final
    post-scale over every cell the effect produces, on top of whatever the
    effect does internally. This is what lets every effect — even ones with
    no built-in brightness-style knob — be tamed down uniformly. Missing or
    out-of-range values are clamped/defaulted rather than raising, since this
    runs on every output tick. Direct calls to a render_* function bypass
    this (by design — see effects.py module docstring for the split)."""
    info = EFFECTS.get(effect_id)
    if info is None:
        raise ValueError(f"Unknown effect: {effect_id}")
    params = params or {}
    cells = info["render"](strip_length, t, params, color_keys)
    try:
        brightness = float(params.get("brightness", 1.0))
    except (TypeError, ValueError):
        brightness = 1.0
    brightness = max(0.0, min(1.0, brightness))
    if brightness >= 1.0:
        return cells
    return [None if c is None else _scale(c, brightness) for c in cells]


def get_registry():
    """JSON-serialisable copy of EFFECTS (without callable render fns) for
    the editor UI to consume. Every effect's params include the universal
    `brightness` control first, followed by its own effect-specific params."""
    out = {}
    for eid, info in EFFECTS.items():
        params = {"brightness": _BRIGHTNESS_PARAM}
        params.update(info["params"])
        out[eid] = {
            "name":           info["name"],
            "description":    info["description"],
            "uses_primary":   info.get("uses_primary", True),
            "uses_secondary": info["uses_secondary"],
            "uses_accent":    info["uses_accent"],
            "params":         params,
        }
    return out


def defaults_for(effect_id):
    """Return a fresh params dict populated with each parameter's default
    (including the universal brightness control), suitable as the starting
    state for a new effect scene in the editor."""
    info = EFFECTS.get(effect_id)
    if info is None:
        return {}
    defaults = {"brightness": _BRIGHTNESS_PARAM["default"]}
    defaults.update({pname: pspec["default"] for pname, pspec in info["params"].items()})
    return defaults
