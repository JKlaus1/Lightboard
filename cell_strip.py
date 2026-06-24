"""
Cell-strip abstraction for the effect engine (Phase 2A).

Every non-mover fixture is exposed as one or more ordered arrays of
"cells" — the smallest addressable color unit at the effect-engine
level. Effects render against cell strips; the strip-to-DMX writer
knows where each cell's color bytes land regardless of the underlying
fixture geometry.

  - A pod-based fixture has ONE cell strip; cells = pods.
    If pod_pixel_count > 1 (legacy WLED-tube-as-pod-fixture), each
    cell's writes target every channel of every pixel in that pod, so
    setting cell N to red lights all of pod N's pixels red.

  - A pixel_strip fixture has explicit `segments`. The strip layout
    depends on the rendering mode:
      MODE_LOCK_TO_SEGMENTS → one strip per segment (reverse-aware).
                              An 8-cell Chase repeats per segment.
      MODE_CONTINUOUS_STRIP → one strip whose cells span all segments
                              in declared order. An 8-cell Chase walks
                              across the entire fixture once.

  - Movers have no cells — build_cell_strips() returns [].

CellStrip dict schema (returned by build_cell_strips):
  {
    "fixture_id":   str,
    "fixture_name": str,
    "label":        str,        # segment name, or fixture name
    "length":       int,        # number of cells in this strip
    "color_keys":   tuple,      # e.g. ('r','g','b','w')
    "writes":       list of lists,
                                # writes[i] = list of (ch_tuple, color_key)
                                # describing every DMX byte that cell i
                                # owns. ch_tuple is (universe, channel)
                                # post-overflow.
  }

The "writes" list is precomputed so effects can render at output_hz
with no per-cell math beyond the color value itself.
"""

# ── Mode constants ──────────────────────────────────────────────────────────

MODE_LOCK_TO_SEGMENTS = "lock_to_segments"
MODE_CONTINUOUS_STRIP = "continuous_strip"
ALL_MODES = (MODE_LOCK_TO_SEGMENTS, MODE_CONTINUOUS_STRIP)


# ── Channel arithmetic ──────────────────────────────────────────────────────

def ch_overflow(start_uni, abs_ch):
    """Map a 1-indexed absolute channel that may exceed 512 to a proper
    (universe, channel) tuple. Channels above 512 spill into the next
    universe. Mirrors engine.LightingEngine._ch_overflow."""
    if abs_ch <= 512:
        return (start_uni, abs_ch)
    universe_offset = (abs_ch - 1) // 512
    ch_in_uni       = ((abs_ch - 1) % 512) + 1
    return (start_uni + universe_offset, ch_in_uni)


# ── Fixture geometry helpers ────────────────────────────────────────────────

def is_pixel_strip(fx):
    return fx.get("type") == "pixel_strip"


def is_mover(fx):
    return fx.get("type") == "mover"


def fx_universe(fx):
    try:
        return int(fx.get("universe", 0))
    except (ValueError, TypeError):
        return 0


def fx_color_offsets(fx):
    """The {color_key: byte_offset} dict describing one pixel/pod's color
    layout. pixel_strip uses pixel_color_offsets; pod fixtures use
    pod_color_offsets."""
    if is_pixel_strip(fx):
        return fx.get("pixel_color_offsets") or {"r":0,"g":1,"b":2,"w":3}
    return fx.get("pod_color_offsets") or {"r":0,"g":1,"b":2,"a":3,"w":4,"uv":5}


def _pod_segments(fx):
    """Synthesize 'segments' for a pod-based fixture. Each pod becomes a
    one-cell segment whose pixel_count is the legacy pod_pixel_count
    (defaulting to 1 for normal pods).

    Returns (segments, bytes_per_pixel) where each segment is:
      {name, first_channel, pixel_count, reversed}
    """
    start_addr  = fx.get("start_address", 1)
    first_pc    = fx.get("first_pod_channel", fx.get("global_channels", 1) + 1)
    ch_per_pod  = fx.get("channels_per_pod", 6)
    n_pods      = fx.get("pods", 1)
    pixels_per  = max(1, int(fx.get("pod_pixel_count", 1)))
    bytes_per_pixel = ch_per_pod // pixels_per if pixels_per > 0 else ch_per_pod

    segs = []
    for pi in range(n_pods):
        pod_base = start_addr + first_pc - 1 + pi * ch_per_pod
        segs.append({
            "name":          f"Pod {pi+1}",
            "first_channel": pod_base,
            "pixel_count":   pixels_per,
            "reversed":      False,
        })
    return segs, bytes_per_pixel


def _pixel_strip_segments(fx):
    """Return (segments, bytes_per_pixel) for a pixel_strip fixture.

    Each segment has absolute first_channel computed from start_address
    + first_pixel_channel + start_index * bytes_per_pixel. If no
    `segments` are declared, the whole strip is treated as one segment.
    """
    start_addr    = fx.get("start_address", 1)
    first_pixel   = fx.get("first_pixel_channel", 1)
    bytes_per_pix = int(fx.get("channels_per_pixel", 4))

    declared = fx.get("segments") or []
    out = []
    for i, seg in enumerate(declared):
        seg_start_idx = int(seg.get("start", 0))
        length        = int(seg.get("length", 0))
        if length <= 0:
            continue
        first_ch = start_addr + first_pixel - 1 + seg_start_idx * bytes_per_pix
        out.append({
            "name":          seg.get("name") or f"Segment {i+1}",
            "first_channel": first_ch,
            "pixel_count":   length,
            "reversed":      bool(seg.get("reversed", False)),
        })

    if not out:
        # No segments declared — treat the whole strip as one segment
        pixel_count = int(fx.get("pixel_count", 0))
        if pixel_count > 0:
            out.append({
                "name":          fx.get("name") or "Strip",
                "first_channel": start_addr + first_pixel - 1,
                "pixel_count":   pixel_count,
                "reversed":      False,
            })
    return out, bytes_per_pix


def fx_segments(fx):
    """Public: return (segments, bytes_per_pixel) for any non-mover
    fixture. Pod fixtures and pixel_strips use the same downstream
    machinery via this unified shape."""
    if is_mover(fx):
        return [], 0
    if is_pixel_strip(fx):
        return _pixel_strip_segments(fx)
    return _pod_segments(fx)


# ── Cell-strip construction ─────────────────────────────────────────────────

def _writes_for_pixel(uni, pixel_first_ch, bpp, color_offsets):
    """List of (ch_tuple, color_key) for ONE physical pixel."""
    out = []
    for key, off in color_offsets.items():
        out.append((ch_overflow(uni, pixel_first_ch + off), key))
    return out


def _build_pixel_cells(uni, seg, bpp, color_offsets):
    """Return cells_writes for a segment treated as one-cell-per-pixel.
    Honors the segment's reversed flag: cell 0 = physical pixel at the
    'far end' if reversed."""
    pixel_count = seg["pixel_count"]
    first_ch    = seg["first_channel"]
    reversed_   = seg["reversed"]
    cells = []
    for cell_idx in range(pixel_count):
        physical_idx = (pixel_count - 1 - cell_idx) if reversed_ else cell_idx
        pixel_first_ch = first_ch + physical_idx * bpp
        cells.append(_writes_for_pixel(uni, pixel_first_ch, bpp, color_offsets))
    return cells


def _make_strip(fx, label, cells_writes, color_offsets):
    return {
        "fixture_id":   fx["id"],
        "fixture_name": fx.get("name", fx["id"]),
        "label":        label,
        "length":       len(cells_writes),
        "color_keys":   tuple(color_offsets.keys()),
        "writes":       cells_writes,
    }


def build_cell_strips(fx, mode=MODE_CONTINUOUS_STRIP):
    """Build cell strip(s) for one fixture in the given rendering mode.

    Returns a list of CellStrip dicts:
      - Pod fixture     → list of 1 strip with `pods` cells. Each cell's
                          writes target every channel of every pixel in
                          the pod (pod_pixel_count replication).
      - Pixel strip
        lock_to_segments → list of N strips (one per segment), each with
                           that segment's pixel_count cells.
        continuous_strip → list of 1 strip with sum-of-segment-pixels
                           cells.
      - Mover           → [].

    Pod fixtures return the same single strip regardless of mode (they
    have no sub-segment concept).
    """
    if is_mover(fx):
        return []

    color_offsets = fx_color_offsets(fx)
    uni           = fx_universe(fx)

    if not is_pixel_strip(fx):
        # POD FIXTURE — one strip, one cell per pod, each cell writes to
        # every pixel of that pod (handles pod_pixel_count > 1 legacy).
        segments, bpp = _pod_segments(fx)
        cells_writes = []
        for seg in segments:
            writes = []
            for px in range(seg["pixel_count"]):
                pixel_first_ch = seg["first_channel"] + px * bpp
                writes.extend(_writes_for_pixel(uni, pixel_first_ch, bpp, color_offsets))
            cells_writes.append(writes)
        return [_make_strip(fx, fx.get("name", fx["id"]), cells_writes, color_offsets)]

    # PIXEL STRIP
    segments, bpp = _pixel_strip_segments(fx)
    if not segments:
        return []

    if mode == MODE_LOCK_TO_SEGMENTS:
        strips = []
        for seg in segments:
            cells_writes = _build_pixel_cells(uni, seg, bpp, color_offsets)
            strips.append(_make_strip(fx, seg["name"], cells_writes, color_offsets))
        return strips

    # MODE_CONTINUOUS_STRIP (default)
    all_cells = []
    for seg in segments:
        all_cells.extend(_build_pixel_cells(uni, seg, bpp, color_offsets))
    return [_make_strip(fx, fx.get("name", fx["id"]), all_cells, color_offsets)]


# ── Cell-strip → DMX writer (used by effects in Phase 2B) ───────────────────

def cell_strip_to_dmx(strip, cell_colors):
    """Convert per-cell colors to a {(universe, channel): value} dict.

    cell_colors[i] is one of:
      - dict like {'r':255,'g':100,'b':0,'w':0}  → that cell is written.
        Missing color keys default to 0.
      - None                                      → that cell is skipped
        entirely so a lower-priority layer can fill it in.

    If cell_colors is shorter than the strip, only the leading cells are
    written. If longer, extra entries are ignored.
    """
    out = {}
    writes_by_cell = strip["writes"]
    n = min(len(cell_colors), len(writes_by_cell))
    for i in range(n):
        color = cell_colors[i]
        if color is None:
            continue
        for ch_tuple, key in writes_by_cell[i]:
            out[ch_tuple] = int(color.get(key, 0))
    return out
