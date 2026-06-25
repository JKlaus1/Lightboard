"""
Lighting engine.
Handles scene playback with per-step fade interpolation,
master dimmer scaling, and singer pod override with crossfade.

Architecture:
  Scene thread   → writes to _scene_dmx (what the scene wants)
  Output thread  → reads _scene_dmx, applies singer + master, writes to DMX
This means master/singer fader changes take effect within ~25ms
even while a scene is in its hold phase.
"""

import gc
import threading
import time
import logging

import cell_strip
import effects
import mover_gen

# ── GC timing instrumentation (low-noise: logs only anomalous pauses) ─────
_GC_CB_INSTALLED = False
def _install_gc_timing():
    """Append a gc.callbacks hook that logs any collection taking >=50ms,
    so we can confirm whether the output-loop hitches are GC pauses. Safe to
    leave installed; it only logs anomalies."""
    global _GC_CB_INSTALLED
    if _GC_CB_INSTALLED:
        return
    _GC_CB_INSTALLED = True
    _t = {"start": 0.0}
    def _cb(phase, info):
        if phase == "start":
            _t["start"] = time.monotonic()
        else:
            dt = (time.monotonic() - _t["start"]) * 1000.0
            if dt >= 50.0:
                logging.getLogger(__name__).warning(
                    "GC pause: gen=%s collected=%s uncollectable=%s %.0fms",
                    info.get("generation"), info.get("collected"),
                    info.get("uncollectable"), dt)
    gc.callbacks.append(_cb)

log = logging.getLogger(__name__)


class LightingEngine:

    OUTPUT_HZ = 40   # how often the output thread updates DMX

    def __init__(self, dmx, show_config):
        self._dmx    = dmx
        self._show   = show_config
        self._lock   = threading.Lock()

        # ── Multi-scene main scene playback ────────────────────────────────
        # Each entry in _active_scenes is a dict with:
        #   id, name, fade (launch_fade), launch_id (unique per play),
        #   thread, stop_event,
        #   dmx (current resolved values, {ch: val}),
        #   owned_channels (set of (uni,ch) this scene controls per its fixtures
        #                   participation - computed once at play time),
        #   start_blend (0→1 fade in), stop_blend (1→0 fade out, when stopping),
        #   stopping (bool)
        self._active_scenes = []     # list of scene_state dicts, oldest first
        self._scene_seq     = 0      # monotonic counter for launch_id

        # Legacy single-slot scene_dmx kept as a *read-only* aggregate view so
        # existing tools (DMX monitor, preview, etc) still work. It's recomputed
        # every output tick from _active_scenes.
        self._scene_dmx = {}

        # Controls
        self._master_level  = 1.0   # 0.0–1.0
        self._singer_mode   = True  # True = override pods with warm white
        self._singer_level  = 1.0   # 0.0–1.0
        # Smooth singer toggle: 0.0 = fully scene, 1.0 = fully singer
        self._singer_blend  = 1.0
        self._singer_target = 1.0
        self._singer_fade_ms = show_config.get("singer_fade_ms", 1500)

        # Live preview slot (separate from active scenes; uses _scene_dmx
        # write path so it appears identical to a playing scene from the
        # output thread's perspective, but doesn't go through multi-scene
        # layering). Set via preview_set() / cleared via preview_clear().
        self._preview_dmx = {}
        self._preview_active = False  # when True, _preview_dmx replaces _scene_dmx

        # Raw channel test override (fixture builder "Test channels" panel).
        # {(universe, ch): value}; when _raw_active it is written on top of the
        # final frame in _push_to_dmx, independent of any defined fixture, so a
        # fixture whose channel layout is unknown can be probed by address.
        # _raw_solo blanks all other patched channels so only the tested
        # addresses emit light. On stop, orphan test addresses (owned by no
        # fixture) are zeroed for a few frames so they go dark instead of
        # latching at their last value in the Art-Net buffer.
        self._raw_override      = {}
        self._raw_active        = False
        self._raw_solo          = False
        self._raw_release_keys  = set()
        self._raw_release_count = 0

        # ── Freeze state ────────────────────────────────────────────────────
        # When frozen, scene change actions (play/stop) are routed to a pending
        # queue instead of immediately taking effect. The live scenes keep
        # playing unchanged. Unfreezing diffs pending vs live and applies all
        # changes at once. freeze_includes controls which scene categories
        # participate in the queue; categories not listed bypass freeze.
        self._freeze_active = False
        # Pending scene queue for main scenes: ordered list of scene_ids that
        # WILL be active after unfreeze. We also keep the scene data for any
        # newly-added scenes so unfreeze can play them without a reload.
        self._pending_main_ids    = []
        self._pending_main_scenes = {}   # scene_id → scene data
        self._pending_main_fades  = {}   # scene_id → launch_fade ms (for new adds)
        # Pending motion/look/effect (Task B: stacked, mirroring main scenes).
        # Each is the ordered list of scene_ids that WILL be active after
        # unfreeze, plus stored scene data for any newly-added-while-frozen ids.
        self._pending_motion_ids    = []
        self._pending_motion_scenes = {}
        self._pending_look_ids      = []
        self._pending_look_scenes   = {}
        self._pending_effect_ids    = []
        self._pending_effect_scenes = {}
        # freeze_includes: which categories queue when freeze is active.
        # Defaults match the user's preferred behavior: scene changes queue,
        # raw control inputs (blackout, dimmers, singer toggle) stay live.
        self._freeze_includes = self._load_freeze_includes(show_config)

        # Blackout: mode + smooth fade blend.
        # mode = None | 'color' | 'full'
        # blend = 0.0 (normal scene) → 1.0 (blackout fully applied)
        self._blackout_mode      = None
        self._blackout_blend     = 0.0
        self._blackout_target    = 0.0
        self._blackout_fade_ms   = show_config.get("blackout_fade_ms", 600)

        # Overlay (e.g. strobe layer that sits on top of the running scene).
        # Plays a scene in a parallel loop; its DMX values replace the main
        # scene's values for any channels it specifies. Fade in/out is smooth.
        self._overlay_dmx          = {}
        self._overlay_blend        = 0.0
        self._overlay_target       = 0.0
        self._overlay_fade_ms      = show_config.get("overlay_fade_ms", 200)
        self._overlay_keep_singer  = show_config.get("overlay_keep_singer", True)
        self._overlay_thread       = None
        self._overlay_stop_event   = threading.Event()
        self._current_overlay_name = None

        # Mover Motion scene layer (Task B: stacked) — controls pan/tilt of
        # mover fixtures. Each entry runs its own player thread writing to its
        # own dmx dict; the layer composite overlays entries in play order
        # (newest on top → latest-wins per channel; different movers union).
        # Entry: {id, name, scene, dmx, thread, stop_event}. No layer-level
        # fade: motion/look snap (step-internal fades still apply).
        self._active_motions      = []
        # Editor preview slot (separate from the stack). While active it
        # OVERRIDES the composited motion layer so the editor shows exactly
        # the frame being edited. Kept out of the stack and the freeze queue.
        self._preview_motion_dmx    = {}
        self._preview_motion_active = False

        # Mover Look scene layer (Task B: stacked) — dimmer/color/gobo/prism
        # etc. of movers. Same stack + preview-slot shape as motion.
        self._active_looks        = []
        self._preview_look_dmx    = {}
        self._preview_look_active = False

        # ── Effect layer (Phase 2C; Task B: stacked) ───────────────────────
        # Each active effect renders animated colour to one or more fixtures'
        # cell strips. Effects are held as a stack of entries, each re-rendered
        # every output tick using its own start_time as the reference for `t`
        # and faded by its own independent blend. The effect layer sits ABOVE
        # the master-scaled main scenes and BELOW singer / overlay / blackout.
        # Stopping fades an entry out via its blend; when its blend reaches 0
        # the entry is removed. Composite = overlay entries in play order.
        # Entry: {id, name, scene, blend, target, start_time}.
        self._active_effects       = []
        self._effect_dmx           = {}    # merged frame, for tools/inspection
        self._effect_fade_ms       = show_config.get("effect_fade_ms", 500)
        # Editor preview slot (single, separate from the stack). While a
        # preview is active it SUPPRESSES the live effect stack so the editor
        # shows only the effect being edited (matches pre-Task-B behaviour).
        self._preview_effect_scene  = None
        self._preview_effect_blend  = 0.0
        self._preview_effect_target = 0.0
        self._preview_effect_start  = 0.0
        # Pending effect changes while frozen use _pending_effect_ids (above).
        # Pending raw-control changes while frozen (None = no queued change).
        self._pending_singer       = None   # bool: desired singer on/off
        self._pending_master       = None   # float: desired color-dimmer level
        self._pending_singer_level = None   # float: desired singer-dimmer level

        # ── Effect colour sampling (Phase 2F) ───────────────────────────────
        # Average non-singer cell colour across all currently-playing main
        # scenes, refreshed once per output tick from the post-composite
        # DMX. Effect scenes can opt into "sample from main scene" per
        # colour slot, in which case _resolve_effect_params substitutes
        # this value at render time. None when no scenes are playing.
        self._sampled_scene_color = None

        # ── Tempo / tap-tempo clock ─────────────────────────────────────────
        self._tempo_active    = False   # master gate; tempo_cancel() clears it
        self._bpm             = 0.0      # committed BPM (0 = none)
        self._beat_anchor     = 0.0      # wall-clock time of a committed downbeat
        self._tap_times       = []       # recent tap timestamps (buffer)
        self._tap_pending     = False    # buffer waiting to settle+commit
        self._tap_preview_bpm = 0.0      # live estimate during a burst (UI only)
        self._tap_settle_s    = show_config.get("tap_settle_s", 0.7)
        self._tap_reset_s     = show_config.get("tap_reset_s", 4.0)
        self._tap_min         = int(show_config.get("tap_min", 3))
        self._bpm_min         = float(show_config.get("bpm_min", 20.0))
        self._bpm_max         = float(show_config.get("bpm_max", 240.0))

        # ── Beat cycler ─────────────────────────────────────────────────────
        self._cycler_active     = False
        self._cycler_scenes     = []      # list of scene dicts to chase
        self._cycler_division   = 1.0     # beats per look
        self._cycler_xfade_ms   = 200     # crossfade between looks
        self._cyc_index         = 0
        self._cyc_deck          = None    # reserved id currently holding the look
        self._cyc_started       = False   # fired the first look this active run?
        self._cyc_last_idx      = 0
        self._cyc_last_clock    = (0.0, 0.0)   # (bpm, anchor) change detector
        self._cycler_thread     = None
        self._cycler_stop_event = threading.Event()

        # Pre-compute channel maps
        self._dimmer_channels   = self._get_dimmer_channels()
        self._singer_channels   = self._get_singer_channels()
        self._singer_dmx_full   = self._build_singer_dmx()
        self._patched_channels  = self._get_all_patched_channels()
        self._rebuild_channel_caches()

        # Pre-compute cell strips for every non-mover fixture in both
        # rendering modes (Phase 2A). Used by main-scene pixel_strip
        # resolution and by effect-scene rendering (Phase 2C).
        self._cell_strips       = self._build_cell_strips_cache()

        # Output-loop timing diagnostics (see _output_loop; low-noise).
        self._tick_durs      = []
        self._tick_window_t  = time.time()
        self._tick_summary_s = 10.0

        # Start output thread
        self._output_running = True
        self._output_thread  = threading.Thread(
            target=self._output_loop, daemon=True, name="dmx-mixer"
        )
        self._output_thread.start()

        # Real-time tuning: confirm/limit GC pauses on the output loop.
        _install_gc_timing()
        self._tune_gc()

    # ── Fixture map helpers ───────────────────────────────────────────────

    def _fx_universe(self, fx):
        """Universe number for a fixture (defaults to 0 for backward compat)."""
        try:
            return int(fx.get("universe", 0))
        except (ValueError, TypeError):
            return 0

    def _ch_overflow(self, start_uni, abs_ch):
        """Convert a 1-indexed absolute channel number that may exceed 512
        into a proper (universe, channel) tuple. Channels above 512 spill
        into the next universe. This lets a fixture span multiple universes
        seamlessly (e.g. a 756-channel WLED pixel strip starting at u2 ch 1
        occupies u2 ch 1-512 + u3 ch 1-244).
        """
        if abs_ch <= 512:
            return (start_uni, abs_ch)
        universe_offset = (abs_ch - 1) // 512
        ch_in_uni = ((abs_ch - 1) % 512) + 1
        return (start_uni + universe_offset, ch_in_uni)

    def _is_mover(self, fx):
        return fx.get("type") == "mover"

    def _is_pixel_strip(self, fx):
        return fx.get("type") == "pixel_strip"

    def _fx_pod_pixel_count(self, fx):
        """How many physical LED pixels each 'pod' represents. Default 1 for
        legacy fixtures (pod = one color cell). For pixel-strip fixtures like
        a WLED tube where each pod represents N pixels (e.g. a side of the
        tube), this is set higher. The pod's color is replicated across all
        N pixels using bytes_per_pixel = channels_per_pod / pod_pixel_count.
        """
        try:
            n = int(fx.get("pod_pixel_count", 1))
            return max(1, n)
        except (ValueError, TypeError):
            return 1

    def _mover_channel_for_role(self, fx, role):
        """Resolve a role name (e.g. 'pan', 'dimmer', 'gobo') to its absolute
        DMX channel number for this fixture. Returns None if not defined."""
        roles = fx.get("channel_roles") or {}
        offset = roles.get(role)
        if offset is None:
            return None
        return fx.get("start_address", 1) + int(offset) - 1

    def _mover_motion_roles(self):
        return ("pan", "pan_fine", "tilt", "tilt_fine")

    def _get_dimmer_channels(self):
        """List of (universe, dmx_ch) for POD fixture master dimmers.
        These are forced to 255 in normal play. Mover fixtures have their own
        dimmer that's controlled by Mover Look scenes — they're NOT in this list.
        """
        out = []
        for fx in self._show["fixtures"]:
            if self._is_mover(fx):
                continue   # mover dimmers are controlled by Look scenes
            d = self._fx_dimmer_offset(fx)
            if d > 0:
                out.append(self._ch_overflow(
                    self._fx_universe(fx),
                    fx["start_address"] + d - 1
                ))
        return out

    def _get_all_patched_channels(self):
        """Set of (universe, dmx_ch) for every patched channel.
        Handles fixtures that span multiple universes via channel overflow."""
        chs = set()
        for fx in self._show.get("fixtures", []):
            uni    = self._fx_universe(fx)
            start  = fx.get("start_address", 1)
            length = fx.get("channels", 1)
            for ch in range(start, start + length):
                if ch >= 1:
                    chs.add(self._ch_overflow(uni, ch))
        return chs

    def _fx_dimmer_offset(self, fx):
        """1-indexed dimmer channel offset within fixture (0 = no dimmer)."""
        if "dimmer_channel" in fx:
            return fx["dimmer_channel"]
        # Legacy fallback
        return 1

    def _fx_first_pod_channel(self, fx):
        """1-indexed offset of pod 1's first channel."""
        if "first_pod_channel" in fx:
            return fx["first_pod_channel"]
        # Legacy
        return (fx.get("global_channels", 1) + 1)

    def _fx_channels_per_pod(self, fx):
        if "channels_per_pod" in fx:
            return fx["channels_per_pod"]
        return 6

    def _fx_pod_color_offsets(self, fx):
        """{color_function: 0-indexed offset within a pod}"""
        if "pod_color_offsets" in fx:
            return fx["pod_color_offsets"]
        # Legacy RGBAWUV default
        return {"r":0,"g":1,"b":2,"a":3,"w":4,"uv":5}

    def _fx_pods(self, fx):
        if "pods" in fx:
            return fx["pods"]
        # Legacy fallback
        if fx.get("type") == "rgbawuv_par":
            return 1
        return 8

    def _get_singer_channels(self):
        """List of (universe, dmx_ch) for singer pod color channels."""
        out = []
        for fx in self._show["fixtures"]:
            # Singer override is a pod-fixture concept. pixel_strip fixtures
            # don't participate (no first_pod_channel/channels_per_pod).
            if self._is_pixel_strip(fx):
                if fx.get("singer_pods"):
                    log.warning("singer_pods set on pixel_strip '%s' — ignored "
                                "(not yet supported)", fx.get("id"))
                continue
            pods = fx.get("singer_pods", [])
            if not pods:
                continue
            uni      = self._fx_universe(fx)
            start    = fx["start_address"]
            first_pc = self._fx_first_pod_channel(fx)
            ch_per   = self._fx_channels_per_pod(fx)
            colors   = self._fx_pod_color_offsets(fx)
            for pod in pods:
                base = start + first_pc - 1 + (pod - 1) * ch_per
                for off in colors.values():
                    out.append(self._ch_overflow(uni, base + off))
        return out

    def _build_singer_dmx(self):
        """Build {(universe, dmx_ch): value} for singer pods at full brightness."""
        sc = self._show.get("singer_color",
                            {"r":20,"g":0,"b":0,"a":200,"w":220,"uv":0})
        result = {}
        for fx in self._show["fixtures"]:
            if self._is_pixel_strip(fx):
                continue  # Singer override not supported on pixel_strip (yet)
            pods = fx.get("singer_pods", [])
            if not pods:
                continue
            uni      = self._fx_universe(fx)
            start    = fx["start_address"]
            first_pc = self._fx_first_pod_channel(fx)
            ch_per   = self._fx_channels_per_pod(fx)
            colors   = self._fx_pod_color_offsets(fx)
            for pod in pods:
                base = start + first_pc - 1 + (pod - 1) * ch_per
                for color, off in colors.items():
                    result[self._ch_overflow(uni, base + off)] = sc.get(color, 0)
        return result

    def resolve_step(self, step_fixtures, scene_type="main"):
        """Convert scene step fixture data → {(universe, dmx_ch): value}.

        scene_type controls which channels can be written:
          'main'         → standard pod-based fixtures (existing behavior)
          'mover_motion' → only pan/tilt channels of mover fixtures
          'mover_look'   → all non-pan/tilt channels of mover fixtures
        """
        dmx = {}
        motion_roles = set(self._mover_motion_roles())
        for fx in self._show["fixtures"]:
            fxid = fx["id"]
            if fxid not in step_fixtures:
                continue
            fd  = step_fixtures[fxid]
            uni = self._fx_universe(fx)

            if self._is_mover(fx):
                if scene_type == "main":
                    continue  # main scenes don't touch movers
                for role, val in fd.items():
                    in_motion = role in motion_roles
                    if scene_type == "mover_motion" and not in_motion:
                        continue
                    if scene_type == "mover_look" and in_motion:
                        continue
                    ch = self._mover_channel_for_role(fx, role)
                    if ch is not None:
                        dmx[self._ch_overflow(uni, ch)] = int(val)
                continue

            # ── pixel_strip fixture (Phase 2A) ─────────────────────────
            # Main scenes can paint a pixel_strip per-segment. The scene
            # step data contains `segments: [color_or_None, ...]` (new
            # canonical) or `pods: [...]` (alias for editor compat).
            # Each entry fills every pixel of that segment with the
            # given colour. Per-pixel control is the job of effect
            # scenes (Phase 2B+), not main scenes.
            if self._is_pixel_strip(fx):
                if scene_type != "main":
                    continue
                seg_data = fd.get("segments")
                if seg_data is None:
                    seg_data = fd.get("pods", [])
                any_active = any(s is not None for s in seg_data)

                # Master dimmer (if defined): only write when this scene
                # actually controls at least one segment of the fixture.
                dim_off = self._fx_dimmer_offset(fx)
                if dim_off > 0 and any_active:
                    dmx[self._ch_overflow(uni, fx["start_address"] + dim_off - 1)] \
                        = fd.get("dimmer", 255)

                # Use the precomputed lock_to_segments strips to know
                # exactly which channels each segment owns.
                seg_strips = self._cell_strips.get(fxid, {}).get(
                    cell_strip.MODE_LOCK_TO_SEGMENTS, []
                )
                for si, strip in enumerate(seg_strips):
                    if si >= len(seg_data):
                        break
                    color = seg_data[si]
                    if color is None:
                        continue
                    # Solid fill: every cell of this segment writes the same color.
                    for cell_writes in strip["writes"]:
                        for ch_tuple, key in cell_writes:
                            dmx[ch_tuple] = int(color.get(key, 0))
                continue

            # Pod-based fixture (only for main scenes)
            if scene_type != "main":
                continue
            start       = fx["start_address"]
            dim_off     = self._fx_dimmer_offset(fx)
            first_pc    = self._fx_first_pod_channel(fx)
            ch_per      = self._fx_channels_per_pod(fx)
            colors      = self._fx_pod_color_offsets(fx)
            n_pods      = self._fx_pods(fx)
            pixels_per  = self._fx_pod_pixel_count(fx)   # 1 for legacy fixtures
            # When a pod represents multiple pixels (e.g. a 63-LED side of a
            # WLED tube), the pod's color gets replicated across all of them.
            # bytes_per_pixel is the channel block size for one pixel within
            # the pod (e.g. 4 for RGBW).
            bytes_per_pixel = ch_per // pixels_per if pixels_per > 0 else ch_per

            pods_data = fd.get("pods")
            if pods_data is None and "color" in fd:
                pods_data = [fd["color"]]
            if pods_data is None:
                pods_data = []

            # Does this scene participate in any pod of this fixture? It does
            # if any pod entry in pods_data is non-None. (An entirely-None list
            # means the fixture is listed but not actually controlled — useful
            # for forward-compat shapes.)
            any_pod_active = any(p is not None for p in pods_data)

            # Write the master dimmer only if the scene actually controls some
            # pod of this fixture. Otherwise some OTHER active scene (or the
            # default 255-via-_get_dimmer_channels) handles the dimmer.
            if dim_off > 0 and any_pod_active:
                dmx[self._ch_overflow(uni, start + dim_off - 1)] = fd.get("dimmer", 255)

            reverse_pods = bool(fx.get("reverse"))
            for pi in range(n_pods):
                if pi >= len(pods_data):
                    break
                pod = pods_data[pi]
                # None = "this pod doesn't participate in this scene" - skip it
                # so a different active scene's value (or zero) takes effect.
                if pod is None:
                    continue
                phys = (n_pods - 1 - pi) if reverse_pods else pi
                pod_base = start + first_pc - 1 + phys * ch_per
                # Write the pod's color to each pixel in the pod. For legacy
                # fixtures (pixels_per=1), this loop executes once and behaves
                # identically to the original code.
                for px in range(pixels_per):
                    pixel_base = pod_base + px * bytes_per_pixel
                    for color, off in colors.items():
                        abs_ch = pixel_base + off
                        dmx[self._ch_overflow(uni, abs_ch)] = pod.get(color, 0)

        return dmx

    # ── Cell-strip cache (Phase 2A) ───────────────────────────────────────
    #
    # Cell strips are an effect-engine abstraction that exposes every
    # non-mover fixture as one or more linear, ordered arrays of cells.
    # See cell_strip.py for the data structure and rendering modes.
    #
    # The cache is built once on init/load_show because the layout only
    # depends on fixture geometry, not on scene playback state.

    def _build_cell_strips_cache(self):
        """Pre-compute cell strips for every non-mover fixture in BOTH
        rendering modes. Returns:
            {fixture_id: {mode: [CellStrip, ...]}}
        Mover fixtures are not included (they have no cells)."""
        cache = {}
        for fx in self._show.get("fixtures", []):
            if fx.get("type") == "mover":
                continue
            fxid = fx["id"]
            built = {
                m: cell_strip.build_cell_strips(fx, mode=m)
                for m in cell_strip.ALL_MODES
            }
            if fx.get("reverse"):
                built = {m: self._reverse_strips(s) for m, s in built.items()}
            cache[fxid] = built
        return cache

    @staticmethod
    def _reverse_strips(strips):
        """Flip a fixture's cell order for 'reverse output' fixtures: reverse
        the cells within each strip and the order of the strips themselves, so
        the fixture renders end-to-end backwards. DMX channel targets are
        untouched — only which cell maps to which physical position changes."""
        out = []
        for strip in reversed(strips):
            s = dict(strip)
            s["writes"] = list(reversed(strip["writes"]))
            out.append(s)
        return out

    def get_cell_strips(self, fixture_id=None, mode=cell_strip.MODE_CONTINUOUS_STRIP):
        """Return cell strips. If fixture_id is None, returns a dict
        {fxid: [strips]} for every non-mover fixture in the given mode.
        Otherwise returns the list of strips for that fixture."""
        if fixture_id is not None:
            return self._cell_strips.get(fixture_id, {}).get(mode, [])
        return {fxid: m.get(mode, []) for fxid, m in self._cell_strips.items()}

    def rebuild_cell_strips(self):
        """Recompute the cell-strip cache in place (e.g. after a fixture's
        'reverse' flag changes) without disrupting playback."""
        with self._lock:
            self._cell_strips = self._build_cell_strips_cache()

    # ── Effect layer (Phase 2C) ───────────────────────────────────────────
    #
    # One effect scene at a time. The scene declares which fixtures
    # participate (fixtures_enabled), which rendering mode to use
    # (lock_to_segments | continuous_strip), an effect id, colour slots
    # (primary/secondary/accent), and per-effect params. Every output
    # tick the effect is re-rendered at the current time and laid over
    # the main-scene composite for the cells it owns.

    def _resolve_effect_params(self, scene):
        """Merge scene's colour slots into params for effects.render().

        Phase 2F: each slot has an optional `<slot>_sample` flag. When set
        AND a main-scene colour sample is currently available, the slot's
        rendered colour is replaced by the sampled value. The slot's static
        colour acts as a fallback when nothing is playing (so the effect
        keeps a sane appearance during silence).
        """
        params = dict(scene.get("params") or {})
        sampled = self._sampled_scene_color   # may be None
        for slot in ("primary", "secondary", "accent"):
            base = scene.get(slot)
            if base is None:
                continue
            if scene.get(slot + "_sample") and sampled is not None:
                params[slot] = sampled
            else:
                params[slot] = base
        return params

    def _effect_fixture_order(self, scene):
        """Ordered fixture-id list an effect renders across. If the scene
        targets a group, use that group's member order (a custom order set on
        the Stage page, independent of show order). Otherwise fall back to show
        order filtered to the scene's fixtures_enabled."""
        gid = scene.get("target_group")
        if gid:
            for g in self._show.get("groups", []):
                if g.get("id") == gid:
                    return list(g.get("members") or [])
        enabled_set = set(scene.get("fixtures_enabled", []))
        return [fx.get("id") for fx in self._show.get("fixtures", [])
                if fx.get("id") in enabled_set]

    def _effect_target_groups(self, scene):
        """Resolved group dicts an effect targets. Supports target_groups (a
        list of ids) and legacy target_group (single id). Returns [] when none
        are set or none match a defined group."""
        ids = scene.get("target_groups")
        if ids is None:
            gid = scene.get("target_group")
            ids = [gid] if gid else []
        by_id = {g.get("id"): g for g in self._show.get("groups", [])}
        return [by_id[i] for i in ids if i in by_id]

    def _render_combined_strip(self, fxids, effect_id, t, params, out):
        """Render `effect_id` across the given fixtures as ONE continuous strip
        at their native combined length (shared clock t), merging DMX writes
        into `out`. Called once per group so several groups run the same effect
        in sync regardless of their individual lengths."""
        combined_writes = []
        combined_keys   = []
        seen_keys       = set()
        for fxid in fxids:
            for strip in self._cell_strips.get(fxid, {}).get(
                    cell_strip.MODE_CONTINUOUS_STRIP, []):
                combined_writes.extend(strip["writes"])
                for k in strip["color_keys"]:
                    if k not in seen_keys:
                        seen_keys.add(k)
                        combined_keys.append(k)
        if not combined_writes:
            return
        cells = effects.render(
            effect_id, len(combined_writes), t, params,
            color_keys=tuple(combined_keys),
        )
        for ch, v in cell_strip.cell_strip_to_dmx(
                {"writes": combined_writes}, cells).items():
            out[ch] = v

    def _render_effect_frame(self, scene, t):
        """Render the effect scene at time `t` into a {(uni,ch): val} dict.
        Walks every fixture in fixtures_enabled, fetches its cell strips
        for the scene's rendering mode, calls effects.render once per
        strip, and merges the per-strip DMX dicts."""
        if scene is None:
            return {}
        effect_id = scene.get("effect")
        if not effect_id or effect_id not in effects.EFFECTS:
            return {}
        mode = scene.get("rendering_mode", cell_strip.MODE_CONTINUOUS_STRIP)
        params = self._resolve_effect_params(scene)
        out = {}
        enabled = scene.get("fixtures_enabled", [])

        if mode == cell_strip.MODE_CONTINUOUS_STRIP:
            # Render the effect across ALL enabled fixtures as ONE continuous
            # strip, so spatial effects (chase, gradient) sweep across the whole
            # rig — single-cell fixtures like PAR cans light up as the pattern
            # passes their position. Sweep order follows the SHOW's fixture
            # order (reorder fixtures in Settings to change it), so physical
            # layout is defined once for the whole rig rather than per scene.
            # No group → render across ALL enabled fixtures as ONE strip in
            # show order. One or more groups → render the SAME effect at the
            # SAME time independently across EACH group's cells, so groups stay
            # in lockstep even with different pod counts (proportional position)
            # and two towers mirror — set 'reverse output' on one of them.
            groups = self._effect_target_groups(scene)
            if groups:
                for g in groups:
                    self._render_combined_strip(g.get("members") or [],
                                                effect_id, t, params, out)
            else:
                self._render_combined_strip(self._effect_fixture_order(scene),
                                            effect_id, t, params, out)
            return out

        # MODE_LOCK_TO_SEGMENTS — each fixture/segment runs the effect
        # independently across its own cells (original behaviour).
        groups = self._effect_target_groups(scene)
        if groups:
            lock_fxids = [fxid for g in groups for fxid in (g.get("members") or [])]
        else:
            lock_fxids = self._effect_fixture_order(scene)
        for fxid in lock_fxids:
            strips = self._cell_strips.get(fxid, {}).get(mode, [])
            for strip in strips:
                cells = effects.render(
                    effect_id, strip["length"], t, params,
                    color_keys=strip["color_keys"],
                )
                for ch, v in cell_strip.cell_strip_to_dmx(strip, cells).items():
                    out[ch] = v
        return out

    def _tick_effect_blend(self):
        """Advance every active effect's fade-in/out blend toward its target,
        plus the editor preview's blend. Entries whose blend reaches 0 (fully
        faded out) are removed; the preview clears its scene at 0."""
        step_full = 1.0 / self.OUTPUT_HZ
        with self._lock:
            fade_ms = self._effect_fade_ms
            step    = step_full / max(fade_ms / 1000.0, 0.05)
            to_remove = []
            for e in self._active_effects:
                target  = e["target"]
                current = e["blend"]
                if abs(target - current) < 0.005:
                    e["blend"] = target
                    if target == 0.0:
                        to_remove.append(e)
                    continue
                direction = 1 if target > current else -1
                e["blend"] = max(0.0, min(1.0, current + direction * step))
            for e in to_remove:
                self._active_effects.remove(e)
            # Preview blend (single slot)
            if self._preview_effect_scene is not None:
                t = self._preview_effect_target
                c = self._preview_effect_blend
                if abs(t - c) < 0.005:
                    self._preview_effect_blend = t
                    if t == 0.0:
                        self._preview_effect_scene = None
                        self._preview_effect_blend = 0.0
                else:
                    d = 1 if t > c else -1
                    self._preview_effect_blend = max(0.0, min(1.0, c + d * step))

    def _effect_active(self, scene_id):
        """True if scene_id is in the effect stack and not fading out."""
        with self._lock:
            return any(e["id"] == scene_id and e["target"] == 1.0
                       for e in self._active_effects)

    def play_effect_scene(self, scene, scene_id=None):
        """Add (or ensure-on) an effect in the stack. Smoothly fades in.
        Re-playing an id already in the stack hot-swaps its data and keeps it
        fading toward on. Queued under freeze when 'effects' is in
        freeze_includes. This is an additive primitive — it never removes
        other effects (presets/Task A rely on that); use stop/toggle for that."""
        if self._is_frozen_for("effects"):
            with self._lock:
                if scene_id is not None and scene_id not in self._pending_effect_ids:
                    self._pending_effect_ids.append(scene_id)
                    self._pending_effect_scenes[scene_id] = scene
            return
        with self._lock:
            for e in self._active_effects:
                if e["id"] == scene_id:
                    # Already in the stack → hot-swap data and ensure fading in.
                    e["scene"]  = scene
                    e["name"]   = scene.get("name")
                    e["target"] = 1.0
                    return
            self._active_effects.append({
                "id":         scene_id,
                "name":       scene.get("name"),
                "scene":      scene,
                "blend":      0.0,
                "target":     1.0,
                "start_time": time.time(),
            })

    def stop_effect_scene(self, scene_id=None):
        """Fade an effect out (scene_id) or all effects (scene_id=None). The
        blend tick removes each entry once its blend reaches 0. Queued under
        freeze: a specific id is dropped from the pending set; None clears it."""
        if self._is_frozen_for("effects"):
            with self._lock:
                if scene_id is None:
                    self._pending_effect_ids = []
                elif scene_id in self._pending_effect_ids:
                    self._pending_effect_ids.remove(scene_id)
            return
        with self._lock:
            for e in self._active_effects:
                if scene_id is None or e["id"] == scene_id:
                    e["target"] = 0.0

    def toggle_effect_scene(self, scene, scene_id=None):
        """Tap-to-toggle: stop this id if it's active, else play it. Freeze-
        aware (toggles the pending set when frozen)."""
        if self._is_frozen_for("effects"):
            with self._lock:
                if scene_id in self._pending_effect_ids:
                    self._pending_effect_ids.remove(scene_id)
                else:
                    self._pending_effect_ids.append(scene_id)
                    self._pending_effect_scenes[scene_id] = scene
            return
        if scene_id is not None and self._effect_active(scene_id):
            self.stop_effect_scene(scene_id)
        else:
            self.play_effect_scene(scene, scene_id=scene_id)

    # ── Effect live preview (editor) ───────────────────────────────────────
    #
    # preview_effect() hot-swaps the scene dict in-place if a preview is
    # already running so editor slider drags don't restart the fade. While a
    # preview is active it SUPPRESSES the live effect stack (see _push_to_dmx)
    # so the editor shows only the effect being edited. Kept out of the stack
    # and the freeze queue.

    PREVIEW_EFFECT_TAG = "__effect_preview__"

    def preview_effect(self, scene):
        """Live-preview an effect scene from the editor. Bypasses freeze.
        If a preview is already running, hot-swap the scene definition
        so timing and fade-in state are preserved (no janky restart)."""
        with self._lock:
            if self._preview_effect_scene is not None:
                self._preview_effect_scene  = scene
                self._preview_effect_target = 1.0   # in case a fade-out had started
                return
            self._preview_effect_scene  = scene
            self._preview_effect_start  = time.time()
            self._preview_effect_target = 1.0

    def preview_effect_clear(self):
        """End the preview. Same fade-out semantics as a normal stop."""
        with self._lock:
            if self._preview_effect_scene is not None:
                self._preview_effect_target = 0.0

    # ── Output loop (mixer) ───────────────────────────────────────────────

    def _rebuild_channel_caches(self):
        """Cache the immutable-after-load channel collections as frozensets so
        the output loop doesn't reconstruct them 40x/sec — that per-tick churn
        is a big contributor to GC pressure (and the transition hitching)."""
        self._singer_channels_set  = frozenset(self._singer_channels)
        self._dimmer_channels_set  = frozenset(self._dimmer_channels)
        self._patched_channels_set = frozenset(self._patched_channels)

    def _tune_gc(self):
        """Keep cyclic-GC gen-2 sweeps from stalling the real-time output loop.
        freeze() moves the big long-lived objects (show config, cell strips,
        channel maps) into a permanent generation the collector never re-scans,
        which collapses gen-2 pause time. unfreeze() first so repeated calls on
        show switch don't pin the previous show's garbage forever."""
        try:
            gc.unfreeze()
        except Exception:
            pass
        gc.collect()
        gc.freeze()
        gc.set_threshold(20000, 100, 100)

    def _output_loop(self):
        """Runs at OUTPUT_HZ. Applies singer + master + overlay + blackout on top of scene DMX."""
        interval = 1.0 / self.OUTPUT_HZ
        while self._output_running:
            t0 = time.time()
            self._tick_active_scenes()
            self._tick_tempo_commit()
            self._tick_singer_blend()
            self._tick_overlay_blend()
            self._tick_blackout_blend()
            self._tick_effect_blend()
            self._push_to_dmx()
            elapsed = time.time() - t0
            ms = elapsed * 1000.0
            if ms > 45.0:
                log.warning("slow output tick: %.0f ms", ms)
            # Rolling window stats — only emitted for windows that actually had
            # an overrun, so a healthy loop stays silent (a quiet journal during
            # a stutter means the hitch is downstream: visualizer poll / WiFi).
            durs = self._tick_durs
            durs.append(ms)
            now = time.time()
            if now - self._tick_window_t >= self._tick_summary_s:
                budget = 1000.0 / self.OUTPUT_HZ
                n  = len(durs)
                sd = sorted(durs)
                mx = sd[-1]
                over = sum(1 for d in sd if d > budget)
                if over > 0 or mx > budget * 1.6:
                    mean = sum(sd) / n
                    p99  = sd[min(n - 1, int(n * 0.99))]
                    log.info("tick health %ds: n=%d mean=%.1fms p99=%.1fms "
                             "max=%.1fms over-%.0fms=%d/%d",
                             int(now - self._tick_window_t), n, mean, p99, mx,
                             budget, over, n)
                self._tick_durs     = []
                self._tick_window_t = now
            sleep = max(0.0, interval - elapsed)
            time.sleep(sleep)

    def _tick_singer_blend(self):
        """Advance singer crossfade blend toward target."""
        with self._lock:
            target = self._singer_target
            current = self._singer_blend
        if abs(target - current) < 0.005:
            with self._lock:
                self._singer_blend = target
            return
        step = (1.0 / self.OUTPUT_HZ) / (self._singer_fade_ms / 1000.0)
        direction = 1 if target > current else -1
        with self._lock:
            self._singer_blend = max(0.0, min(1.0, current + direction * step))

    def _tick_blackout_blend(self):
        """Advance blackout fade blend toward target. Clear mode when fully off."""
        with self._lock:
            target  = self._blackout_target
            current = self._blackout_blend
            fade_ms = self._blackout_fade_ms
        if abs(target - current) < 0.005:
            with self._lock:
                self._blackout_blend = target
                # Once fully faded out, clear the mode so output uses normal path
                if target == 0.0 and self._blackout_mode is not None:
                    self._blackout_mode = None
            return
        step = (1.0 / self.OUTPUT_HZ) / max(fade_ms / 1000.0, 0.05)
        direction = 1 if target > current else -1
        with self._lock:
            self._blackout_blend = max(0.0, min(1.0, current + direction * step))

    def _tick_overlay_blend(self):
        """Advance overlay fade blend toward target. When fully faded out,
        stop the overlay scene thread and clear overlay state."""
        with self._lock:
            target  = self._overlay_target
            current = self._overlay_blend
            fade_ms = self._overlay_fade_ms
        if abs(target - current) < 0.005:
            with self._lock:
                self._overlay_blend = target
                # Fully faded out: stop the overlay scene player
                if target == 0.0 and self._overlay_thread is not None:
                    self._overlay_stop_event.set()
                    self._overlay_dmx          = {}
                    self._current_overlay_name = None
                    # Note: thread will exit on its own next loop check;
                    # don't join here to avoid deadlocking the output thread
                    self._overlay_thread       = None
            return
        step = (1.0 / self.OUTPUT_HZ) / max(fade_ms / 1000.0, 0.05)
        direction = 1 if target > current else -1
        with self._lock:
            self._overlay_blend = max(0.0, min(1.0, current + direction * step))

    def _push_to_dmx(self):
        """Build the final DMX frame.

        Pipeline (in order):
          1. Start with all patched channels at 0 so old scene values
             don't linger when a scene clears a channel.
          2. Apply scene values; scale non-singer color channels by Color Dimmer.
          2.5 Effect layer (Phase 2C): renders animated colour on the
              fixtures named in the active effect scene's fixtures_enabled
              list, overriding the main-scene values on those cells.
              Singer / overlay / blackout still win over the effect.
          3. Blend singer-color override onto singer channels (scaled by Singer Dimmer).
          4. Force fixture dimmer channels to 255.
          5. Apply blackout filter (smoothly faded by _blackout_blend):
               - 'color': non-singer color channels → 0
               - 'full' : every patched channel → 0
        Result is sent in one shot so previously-set channels don't persist.
        """
        # Compute composite of all active main scenes (or preview override)
        if self._preview_active:
            scene = dict(self._preview_dmx)
        else:
            scene = self._composite_scene_dmx()
        # Phase 2F: refresh sampled colour for any effect slots set to
        # "sample from scene". Cheap; reads back through cell strips.
        self._update_scene_color_sample(scene)
        # Keep the legacy _scene_dmx field in sync for tools that read it
        # directly (DMX monitor's snapshot path, etc.)
        with self._lock:
            self._scene_dmx = scene
            # Stacked motion/look: composite each layer (preview overrides).
            if self._preview_motion_active:
                motion = dict(self._preview_motion_dmx)
            else:
                motion = {}
                for e in self._active_motions:
                    motion.update(e["dmx"])          # play order, latest wins
            if self._preview_look_active:
                look = dict(self._preview_look_dmx)
            else:
                look = {}
                for e in self._active_looks:
                    look.update(e["dmx"])
            sb            = self._singer_blend
            singer        = self._singer_dmx_full   # static ref (read-only; never mutated in place)
            s_level       = self._singer_level
            master        = self._master_level
            s_chs         = self._singer_channels_set
            d_chs         = self._dimmer_channels_set
            patched       = self._patched_channels_set
            bo_mode       = self._blackout_mode
            bo_blend      = self._blackout_blend
            ov_dmx        = dict(self._overlay_dmx)
            ov_blend      = self._overlay_blend
            ov_keep_singer = self._overlay_keep_singer
            # Stacked effects: snapshot (scene, blend, start) per entry. While
            # an editor preview is active it SUPPRESSES the live stack so the
            # editor shows only the effect being edited.
            if self._preview_effect_scene is not None and self._preview_effect_blend > 0.001:
                effect_specs = [(self._preview_effect_scene,
                                 self._preview_effect_blend,
                                 self._preview_effect_start)]
            else:
                effect_specs = [(e["scene"], e["blend"], e["start_time"])
                                for e in self._active_effects if e["blend"] > 0.001]

        # Render each active effect outside the lock — scene dicts are replaced
        # atomically by play/stop, never mutated in place, so this is safe.
        # Cell strips are immutable after build. Build an ordered list of
        # (frame, blend) so steps 3b/4 can composite them in play order.
        effect_frames = []
        merged_effect = {}
        for e_scene, e_blend, e_start in effect_specs:
            if e_scene.get("tempo_sync") and self._tempo_active:
                # Musical time: beats since anchor, scaled by the per-effect beat
                # window so the effect's "speed" reads as cycles per <division>
                # beats (1 = cycles/beat, 2 = cycles per 2 beats, 0.5 = per ½ beat).
                division = float(e_scene.get("beat_division", 1.0)) or 1.0
                t_eff = self.beat_time() / division
            else:
                t_eff = time.time() - e_start  # default: wall-clock seconds
            frame = self._render_effect_frame(e_scene, t_eff)
            if frame:
                effect_frames.append((frame, e_blend))
                merged_effect.update(frame)
        # Cache merged frame for tools/inspection; harmless under read races.
        self._effect_dmx = merged_effect

        # 1. Initialize every patched channel to 0
        normal = {ch: 0.0 for ch in patched}

        # 2. Lay main scene values on top
        for ch, val in scene.items():
            normal[ch] = float(val)

        # 2b. Lay mover Look values (dimmer, color, gobo, etc. of movers)
        for ch, val in look.items():
            normal[ch] = float(val)

        # 2c. Lay mover Motion values (pan/tilt of movers) — these overlay look
        for ch, val in motion.items():
            normal[ch] = float(val)

        # 3. Scale non-singer color channels by Color Dimmer
        for ch in list(normal.keys()):
            if ch in s_chs or ch in d_chs:
                continue
            normal[ch] *= master

        # 3b. Effect layer (Phase 2C; Task B stacked). Each active effect owns
        #     the cells of the fixtures it lists, so its values replace whatever
        #     is below on those channels; entries composite in play order
        #     (newest on top). Effects are NOT scaled by master. Singer pods are
        #     included here so the effect value is written into normal[ch] —
        #     step 4 then crossfades that toward the singer color. When singer is
        #     OFF (sb=0) the effect shows through; when ON (sb=1) singer wins.
        for frame, e_blend in effect_frames:
            for ch, ev in frame.items():
                cur = normal.get(ch, 0.0)
                normal[ch] = cur + (float(ev) - cur) * e_blend

        # 4. Singer pod channels: crossfade (main_scene+effects)↔singer-color.
        #    The "below" value folds in every active effect (in play order) so
        #    turning the singer off reveals the composited effect (or main scene
        #    if none are running) rather than leaving those pods dark. With a
        #    single effect this reduces exactly to the pre-Task-B math.
        for ch in s_chs:
            below = scene.get(ch, 0) * master   # master-scaled main value
            for frame, e_blend in effect_frames:
                if ch in frame:
                    below = below + (float(frame[ch]) - below) * e_blend
            singer_val = singer.get(ch, 0) * s_level
            normal[ch] = below + (singer_val - below) * sb

        # 5. Overlay blend (e.g. strobe layer). Where overlay defines a channel,
        #    blend toward overlay value by ov_blend. Overlay runs at full
        #    intensity (no Color Dimmer scaling).
        #    If overlay_keep_singer is True, the overlay is NOT applied to
        #    singer pod channels — they continue to follow the normal singer
        #    behavior so the vocalist stays lit during strobes.
        if ov_blend > 0.001:
            for ch, ov_val in ov_dmx.items():
                if ov_keep_singer and ch in s_chs:
                    continue
                if ch not in normal:
                    normal[ch] = 0.0
                cur = normal[ch]
                normal[ch] = cur + (float(ov_val) - cur) * ov_blend

        # 6. Fixture dimmers always at max
        for ch in d_chs:
            normal[ch] = 255.0

        # 7. Build blackout target frame (what output looks like at blend=1)
        if bo_mode == 'full':
            target = {ch: 0.0 for ch in normal}
        elif bo_mode == 'color':
            target = dict(normal)
            for ch in list(target.keys()):
                if ch in s_chs or ch in d_chs:
                    continue
                target[ch] = 0.0
        else:
            target = normal  # no blackout active

        # 8. Blend normal → target by bo_blend
        if bo_blend <= 0.001 or bo_mode is None:
            frame = normal
        elif bo_blend >= 0.999:
            frame = target
        else:
            frame = {}
            for ch in normal:
                n = normal[ch]
                t = target.get(ch, n)
                frame[ch] = n + (t - n) * bo_blend

        # 8b. Raw channel test override (fixture builder discovery panel).
        #     Highest-priority layer, independent of any defined fixture so an
        #     unknown fixture can be probed by address. In solo mode every
        #     other patched channel is blanked first so only the addresses
        #     under test emit light.
        if self._raw_active:
            with self._lock:
                raw  = dict(self._raw_override)
                solo = self._raw_solo
            if solo:
                frame = {ch: 0.0 for ch in frame}
            for k, v in raw.items():
                frame[k] = float(v)

        # 9. Send (rounded, clipped). Group by universe so the driver can
        #    route each universe to the right Art-Net packet (or ignore
        #    non-zero universes if it's a single-universe driver like Enttec).
        output = {}
        for key, v in frame.items():
            uni, ch = key
            output.setdefault(uni, {})[ch] = max(0, min(255, int(round(v))))
        # Release orphan test addresses (driven only by a stopped raw test and
        # owned by no fixture) by sending 0 for a few frames so they go dark.
        if self._raw_release_count > 0:
            with self._lock:
                rel = list(self._raw_release_keys)
                self._raw_release_count -= 1
                if self._raw_release_count <= 0:
                    self._raw_release_keys = set()
            for runi, rch in rel:
                output.setdefault(runi, {}).setdefault(rch, 0)
        self._dmx.set_channels(output)

    # ── Scene playback ────────────────────────────────────────────────────

    # ── Multi-scene playback ──────────────────────────────────────────────
    #
    # Each call to play_scene() appends a new active scene to _active_scenes.
    # Scenes layer with "latest wins per channel" — when two scenes both write
    # to the same (uni, ch), the one added later takes effect. Each scene runs
    # its own playback thread that walks through its steps and produces a live
    # dict of values in scene_state['dmx'].
    #
    # Starting a scene fades in (start_blend ramps 0→1) over the scene's
    # launch_fade. Stopping fades out (stop_blend ramps 1→0). When stop_blend
    # reaches 0 the scene is removed from _active_scenes.

    def _new_scene_state(self, scene, scene_id, fade_ms):
        self._scene_seq += 1
        return {
            "id":           scene_id,
            "name":         scene.get("name"),
            "fade":         fade_ms,
            "launch_id":    self._scene_seq,
            "scene":        scene,
            "dmx":          {},
            "thread":       None,
            "stop_event":   threading.Event(),
            "start_blend":  0.0,   # ramps 0→1 while fading in
            "stop_blend":   1.0,   # ramps 1→0 while fading out (then removed)
            "stopping":     False, # True once stop_scene() has been called
            "start_time":   time.time(),
        }

    def play_scene(self, scene, launch_fade_ms=None, scene_id=None, force=False):
        """Add a new active scene to the layered playback stack.
        If a scene with the same scene_id is already active, it is stopped
        and replaced (so re-tapping the same scene cleanly restarts it).
        When freeze is active and 'main_scenes' is in freeze_includes, the
        action is queued instead of taking effect immediately."""
        # Resolve the fade
        fade = scene.get("launch_fade")
        if fade is None:
            fade = launch_fade_ms if launch_fade_ms is not None \
                   else self._show.get("default_launch_fade", 500)
        fade = max(0, int(fade))

        # Freeze gate: if scene-category changes are queued, route to pending
        if self._is_frozen_for("main_scenes") and not force:
            with self._lock:
                if scene_id is not None:
                    # Tap-to-toggle while frozen: if it was already in pending,
                    # remove it; otherwise add to end (top of layer stack).
                    if scene_id in self._pending_main_ids:
                        self._pending_main_ids.remove(scene_id)
                        # Don't drop the scene data — they might tap it again
                    else:
                        self._pending_main_ids.append(scene_id)
                        self._pending_main_scenes[scene_id] = scene
                        self._pending_main_fades[scene_id]  = fade
            return

        # Stop any existing instance of this scene_id
        if scene_id is not None:
            self._stop_scene_by_id(scene_id, immediate=True)

        st = self._new_scene_state(scene, scene_id, fade)
        with self._lock:
            self._active_scenes.append(st)
            # Release any active blackout — fade back to normal as scene takes over
            self._blackout_target = 0.0

        st["thread"] = threading.Thread(
            target=self._scene_loop,
            args=(st,),
            daemon=True,
            name=f"scene-player-{st['launch_id']}",
        )
        st["thread"].start()

    def refresh_active_scene(self, scene_id, scene):
        """If `scene_id` is a currently-active MAIN scene, restart it in place
        with the updated data and no launch fade, so editing-and-saving shows
        immediately without toggling the scene off/on. Returns False (no-op)
        when the scene isn't currently playing."""
        if scene_id is None:
            return False
        with self._lock:
            active = any(s.get("id") == scene_id and not s.get("stopping")
                         for s in self._active_scenes)
        if not active:
            return False
        sc = dict(scene)
        sc["launch_fade"] = 0          # snap to the new look
        self.play_scene(sc, launch_fade_ms=0, scene_id=scene_id, force=True)
        return True

    def refresh_active_effect(self, scene_id, scene):
        """If `scene_id` is an active EFFECT in the stack, hot-swap its data
        live (no re-fade) so an edit-and-save updates immediately. Returns
        False (no-op) when it isn't currently an active effect."""
        if scene_id is None:
            return False
        with self._lock:
            found = any(e["id"] == scene_id and e["target"] == 1.0
                        for e in self._active_effects)
        if not found:
            return False
        self.play_effect_scene(scene, scene_id=scene_id)   # hot-swaps the entry
        return True

    def stop_scene(self, scene_id=None):
        """Stop a scene (or all scenes when scene_id is None). Stopping is a
        graceful fade-out — the scene stays in the layered stack until its
        stop_blend reaches 0, then is removed.
        When freeze is active and 'main_scenes' is in freeze_includes, the
        action is queued instead of taking effect immediately."""
        # Freeze gate
        if self._is_frozen_for("main_scenes"):
            with self._lock:
                if scene_id is None:
                    # "Stop all" while frozen clears the pending list entirely
                    self._pending_main_ids = []
                else:
                    if scene_id in self._pending_main_ids:
                        self._pending_main_ids.remove(scene_id)
            return

        if scene_id is None:
            with self._lock:
                ids = [s["id"] for s in self._active_scenes if s["id"] is not None]
            for sid in ids:
                self._stop_scene_by_id(sid, immediate=False)
        else:
            self._stop_scene_by_id(scene_id, immediate=False)

    def stop_all_scenes(self):
        """Convenience: hard-stop all main scenes immediately."""
        with self._lock:
            scenes = list(self._active_scenes)
            self._active_scenes = []
        for st in scenes:
            st["stop_event"].set()
            if st["thread"] and st["thread"].is_alive():
                st["thread"].join(timeout=0.5)

    def _stop_scene_by_id(self, scene_id, immediate=False):
        """Mark a scene as stopping. If immediate=True, removes it from the
        stack right away (no fade-out)."""
        with self._lock:
            target = None
            for st in self._active_scenes:
                if st["id"] == scene_id:
                    target = st
                    break
            if target is None:
                return
            if immediate:
                target["stop_event"].set()
                self._active_scenes.remove(target)
            else:
                if not target["stopping"]:
                    target["stopping"] = True
                    target["stop_event"].set()    # halt the playback thread
        if immediate and target["thread"] and target["thread"].is_alive():
            target["thread"].join(timeout=0.5)

    def _scene_loop(self, state):
        """Drive one scene's step sequence into state['dmx'] until stop_event.
        When the scene is tempo-synced AND a tempo is live, steps advance on the
        beat grid instead of their authored ms hold; otherwise unchanged."""
        scene = state["scene"]
        steps = scene.get("steps", [])
        if not steps:
            return
        first = True
        while not state["stop_event"].is_set():
            synced   = bool(scene.get("tempo_sync"))
            division = float(scene.get("beat_division", 1.0))
            for step in steps:
                if state["stop_event"].is_set():
                    return
                target = self.resolve_step(step.get("fixtures", {}), scene_type="main")
                # First step uses launch_fade; later steps use their own fade
                fade = state["fade"] if first else step.get("fade", 0)
                hold = step.get("hold", 500)
                first = False
                if synced and self._tempo_active:
                    self._execute_step_synced(state, target, fade, division, hold)
                else:
                    self._execute_step_for(state, target, fade, hold)
                if state["stop_event"].is_set():
                    return

    def _fade_dmx(self, state, target_dmx, duration_s):
        """Interpolate state['dmx'] -> target_dmx over duration_s seconds,
        updating under the lock so the output thread sees consistent frames.
        duration_s <= 0 snaps immediately. Wakes early on stop_event."""
        start_dmx = dict(state["dmx"])
        for ch in target_dmx:
            if ch not in start_dmx:
                start_dmx[ch] = 0
        if duration_s <= 0:
            with self._lock:
                state["dmx"] = dict(target_dmx)
            return
        t0 = time.time()
        while not state["stop_event"].is_set():
            t = min((time.time() - t0) / duration_s, 1.0)
            frame = {ch: int(start_dmx.get(ch, 0) + (tgt - start_dmx.get(ch, 0)) * t)
                     for ch, tgt in target_dmx.items()}
            with self._lock:
                state["dmx"] = frame
            if t >= 1.0:
                break
            time.sleep(0.02)

    def _execute_step_for(self, state, target_dmx, fade_ms, hold_ms):
        """Default ms-timed step: fade over fade_ms, then hold for hold_ms."""
        self._fade_dmx(state, target_dmx, fade_ms / 1000.0 if fade_ms > 0 else 0.0)
        if not state["stop_event"].is_set():
            state["stop_event"].wait(timeout=hold_ms / 1000.0)

    def _execute_step_synced(self, state, target_dmx, fade_ms, division, hold_ms):
        """Tempo-synced step: fade to target (capped to the beat slot), then hold
        until the next beat-grid boundary so steps land on the beat. If tempo
        drops out mid-step, reverts to the authored ms fade+hold."""
        now = time.time()
        boundary = self.next_beat_boundary(now, division)
        if boundary is not None and (boundary - now) < 0.05:
            # Avoid a degenerate ultra-short slot (first step landing just before
            # a beat) — take the following boundary instead.
            boundary = self.next_beat_boundary(boundary, division)
        if boundary is None:
            # Tempo vanished between the loop's check and here — revert to ms.
            self._execute_step_for(state, target_dmx, fade_ms, hold_ms)
            return
        slot   = max(0.0, boundary - now)
        fade_s = min(fade_ms / 1000.0, slot) if fade_ms > 0 else 0.0
        self._fade_dmx(state, target_dmx, fade_s)
        remaining = boundary - time.time()
        if remaining > 0 and not state["stop_event"].is_set():
            state["stop_event"].wait(timeout=remaining)

    def _tick_active_scenes(self):
        """Advance each active scene's start_blend / stop_blend toward its
        target. Remove scenes whose stop_blend has reached 0. Called once
        per output tick."""
        interval = 1.0 / self.OUTPUT_HZ
        to_remove = []
        with self._lock:
            for st in self._active_scenes:
                fade = max(st["fade"], 1)   # avoid div-by-zero
                step = interval / (fade / 1000.0)
                if st["stopping"]:
                    st["stop_blend"] = max(0.0, st["stop_blend"] - step)
                    if st["stop_blend"] <= 0.0:
                        to_remove.append(st)
                else:
                    if st["start_blend"] < 1.0:
                        st["start_blend"] = min(1.0, st["start_blend"] + step)
            for st in to_remove:
                self._active_scenes.remove(st)
        # Join the threads of removed scenes (outside lock to avoid deadlock)
        for st in to_remove:
            if st["thread"] and st["thread"].is_alive():
                st["thread"].join(timeout=0.1)

    def _composite_scene_dmx(self):
        """Build the combined main-scene DMX dict by stacking all active
        scenes in start-order (oldest first). Each scene's values are scaled
        by its effective blend = start_blend * stop_blend so fade in/out is
        smooth. Where two scenes both write a channel, the later one's blend
        weights it on top of the earlier composite — giving 'latest wins' for
        steady-state but smooth crossfades during transitions."""
        composite = {}
        with self._lock:
            snapshot = []
            for st in self._active_scenes:
                blend = st["start_blend"] * st["stop_blend"]
                if blend <= 0.0:
                    continue
                # Copy dmx under lock to ensure a consistent snapshot
                snapshot.append((blend, dict(st["dmx"])))
        # Lay each scene's values on top of the running composite, weighted
        # by blend. For full-blend scenes (blend=1.0) this is a straight
        # overwrite; for partial blends it's an interpolated crossfade.
        for blend, sd in snapshot:
            for ch, v in sd.items():
                if ch in composite:
                    cur = composite[ch]
                    composite[ch] = cur + (v - cur) * blend
                else:
                    # No previous value → fade from 0 to v
                    composite[ch] = v * blend
        # Return as ints (DMX values)
        return {ch: int(round(v)) for ch, v in composite.items()}

    # ── Phase 2F: sample colour from running main scene ───────────────────

    def _update_scene_color_sample(self, composite_dmx):
        """Refresh _sampled_scene_color from the current main-scene
        composite. Averages the colour of every non-singer cell across
        every non-mover fixture, excluding the cells the active effect
        is rendering on (so the effect doesn't sample its own output).

        Called once per output tick from _push_to_dmx. Cheap — touches
        roughly one cell strip per fixture and a handful of dict lookups
        per cell.
        """
        if not composite_dmx:
            self._sampled_scene_color = None
            return

        # Exclude the cells every active effect (and the editor preview) renders
        # on, so effects don't sample their own output. Union across the stack.
        exclude = set()
        with self._lock:
            for e in self._active_effects:
                exclude |= set(e["scene"].get("fixtures_enabled") or [])
            if self._preview_effect_scene is not None:
                exclude |= set(self._preview_effect_scene.get("fixtures_enabled") or [])

        accum = {}
        count = 0
        for fx in self._show.get("fixtures", []):
            if fx["id"] in exclude:
                continue
            if self._is_mover(fx):
                continue
            # MODE_CONTINUOUS_STRIP gives one strip per fixture: one cell
            # per pod (pod fixtures) or one per pixel (pixel_strip).
            strips = self._cell_strips.get(fx["id"], {}).get(
                cell_strip.MODE_CONTINUOUS_STRIP, []
            )
            singer_pods = set(fx.get("singer_pods") or [])
            is_pixel    = self._is_pixel_strip(fx)
            for strip in strips:
                for cell_idx, cell_writes in enumerate(strip["writes"]):
                    # Pod fixtures: skip cells that ARE singer pods (1-indexed)
                    if not is_pixel and (cell_idx + 1) in singer_pods:
                        continue
                    # Read this cell's colour back from the composite.
                    cell_color = {}
                    for ch_tuple, key in cell_writes:
                        cell_color[key] = composite_dmx.get(ch_tuple, 0)
                    if not cell_color:
                        continue
                    # Skip cells that are entirely dark — they're probably
                    # not being controlled by any scene and would just drag
                    # the average toward black.
                    if all(v == 0 for v in cell_color.values()):
                        continue
                    for k, v in cell_color.items():
                        accum[k] = accum.get(k, 0) + v
                    count += 1

        if count == 0:
            self._sampled_scene_color = None
        else:
            self._sampled_scene_color = {k: int(v / count) for k, v in accum.items()}

    def get_active_scenes(self):
        """Return a list of currently-active main scenes (oldest first).
        Each entry: {id, name, stopping}.
        Used by the UI to render highlighted active scenes on the home page."""
        with self._lock:
            return [
                {"id": s["id"], "name": s["name"], "stopping": s["stopping"]}
                for s in self._active_scenes
            ]

    # ── Freeze (pending change queue) ──────────────────────────────────────

    DEFAULT_FREEZE_INCLUDES = {
        "main_scenes":  True,
        "motion":       True,
        "look":         True,
        "effects":      True,
        "overlay":      False,
        "blackout":     False,
        "color_dimmer": False,
        "singer_mode":  False,
        "singer_dimmer": False,
    }

    def _load_freeze_includes(self, show_config):
        """Merge per-show freeze_includes settings with defaults."""
        cfg = show_config.get("freeze_includes") or {}
        merged = dict(self.DEFAULT_FREEZE_INCLUDES)
        for k, v in cfg.items():
            if k in merged:
                merged[k] = bool(v)
        return merged

    def _is_frozen_for(self, category):
        """True if freeze is active AND this category should queue."""
        return self._freeze_active and self._freeze_includes.get(category, False)

    def set_freeze(self, enable):
        """Toggle freeze on/off. On enabling, seed pending lists from live
        state so 'no changes' is the default. On disabling, diff pending vs
        live and apply: stop removed scenes, start added scenes, apply pending
        motion/look transitions."""
        enable = bool(enable)
        if enable:
            self._enter_freeze()
        else:
            self._exit_freeze()

    def _enter_freeze(self):
        if self._freeze_active:
            return  # idempotent
        with self._lock:
            # Seed pending lists with current live items (in order)
            self._pending_main_ids = [
                s["id"] for s in self._active_scenes
                if s["id"] is not None and not s["stopping"]
            ]
            self._pending_motion_ids = [
                e["id"] for e in self._active_motions if e["id"] is not None
            ]
            self._pending_look_ids = [
                e["id"] for e in self._active_looks if e["id"] is not None
            ]
            self._pending_effect_ids = [
                e["id"] for e in self._active_effects
                if e["id"] is not None and e["target"] == 1.0
            ]
            self._pending_motion_scenes = {}
            self._pending_look_scenes   = {}
            self._pending_effect_scenes = {}
            self._pending_singer = None
            self._pending_master = None
            self._pending_singer_level = None
            self._freeze_active = True
        log.info("Freeze ENABLED; pending seeded: %d main, %d motion, %d look, %d effect",
                 len(self._pending_main_ids), len(self._pending_motion_ids),
                 len(self._pending_look_ids), len(self._pending_effect_ids))

    def _exit_freeze(self):
        if not self._freeze_active:
            return
        # Snapshot pending state, then clear flag so play/stop calls go live
        with self._lock:
            pending_main_ids = list(self._pending_main_ids)
            pending_main_scenes = dict(self._pending_main_scenes)
            pending_main_fades  = dict(self._pending_main_fades)
            pending_motion_ids    = list(self._pending_motion_ids)
            pending_motion_scenes = dict(self._pending_motion_scenes)
            pending_look_ids      = list(self._pending_look_ids)
            pending_look_scenes   = dict(self._pending_look_scenes)
            pending_effect_ids    = list(self._pending_effect_ids)
            pending_effect_scenes = dict(self._pending_effect_scenes)
            pending_singer = self._pending_singer
            pending_master = self._pending_master
            pending_singer_level = self._pending_singer_level
            live_ids = [
                s["id"] for s in self._active_scenes
                if s["id"] is not None and not s["stopping"]
            ]
            live_motion = [e["id"] for e in self._active_motions if e["id"] is not None]
            live_look   = [e["id"] for e in self._active_looks   if e["id"] is not None]
            live_effect = [e["id"] for e in self._active_effects
                           if e["id"] is not None and e["target"] == 1.0]
            self._freeze_active = False
            self._pending_main_ids    = []
            self._pending_main_scenes = {}
            self._pending_main_fades  = {}
            self._pending_motion_ids    = []
            self._pending_motion_scenes = {}
            self._pending_look_ids      = []
            self._pending_look_scenes   = {}
            self._pending_effect_ids    = []
            self._pending_effect_scenes = {}
            self._pending_singer = None
            self._pending_master = None
            self._pending_singer_level = None
            # Releasing freeze cancels any active blackout - the new look
            # should be visible
            self._blackout_target = 0.0

        # Apply diff for main scenes
        pending_set = set(pending_main_ids)
        live_set    = set(live_ids)
        for sid in [i for i in live_ids if i not in pending_set]:
            self.stop_scene(scene_id=sid)
        for sid in [i for i in pending_main_ids if i not in live_set]:
            scene = pending_main_scenes.get(sid)
            fade  = pending_main_fades.get(sid)
            if scene is not None:
                self.play_scene(scene, launch_fade_ms=fade, scene_id=sid)

        # Apply diff for motion / look / effect stacks (same shape)
        def _apply_diff(live, pending_ids, pending_scenes, stop_fn, play_fn):
            pset, lset = set(pending_ids), set(live)
            for sid in [i for i in live if i not in pset]:
                stop_fn(sid)
            for sid in [i for i in pending_ids if i not in lset]:
                sc = pending_scenes.get(sid)
                if sc is not None:
                    play_fn(sc, sid)

        _apply_diff(live_motion, pending_motion_ids, pending_motion_scenes,
                    lambda sid: self.stop_motion_scene(sid),
                    lambda sc, sid: self.play_motion_scene(sc, scene_id=sid))
        _apply_diff(live_look, pending_look_ids, pending_look_scenes,
                    lambda sid: self.stop_look_scene(sid),
                    lambda sc, sid: self.play_look_scene(sc, scene_id=sid))
        _apply_diff(live_effect, pending_effect_ids, pending_effect_scenes,
                    lambda sid: self.stop_effect_scene(sid),
                    lambda sc, sid: self.play_effect_scene(sc, scene_id=sid))

        # Apply queued raw-control changes (freeze now inactive → live)
        if pending_singer is not None:
            self.set_singer_mode(pending_singer)
        if pending_master is not None:
            self.set_master(pending_master)
        if pending_singer_level is not None:
            self.set_singer_level(pending_singer_level)

        log.info("Freeze DISABLED; applied main diff %d/%d, motion %d, look %d, effect %d",
                 len([i for i in live_ids if i not in pending_set]),
                 len([i for i in pending_main_ids if i not in live_set]),
                 len(pending_motion_ids), len(pending_look_ids), len(pending_effect_ids))

    def get_freeze_state(self):
        """Return current freeze state including pending changes for UI display."""
        with self._lock:
            return {
                "active": self._freeze_active,
                "includes": dict(self._freeze_includes),
                "pending_main_ids":   list(self._pending_main_ids),
                "pending_motion_ids": list(self._pending_motion_ids),
                "pending_look_ids":   list(self._pending_look_ids),
                "pending_effect_ids": list(self._pending_effect_ids),
                "pending_singer": self._pending_singer,
                "pending_master": self._pending_master,
                "pending_singer_level": self._pending_singer_level,
            }

    # ── Controls ──────────────────────────────────────────────────────────

    def set_master(self, level):
        """Set the Color Dimmer (master) level. Queued under freeze when
        'color_dimmer' is in freeze_includes, instead of breaking through."""
        level = max(0.0, min(1.0, float(level)))
        if self._is_frozen_for("color_dimmer"):
            with self._lock:
                self._pending_master = level
            return
        with self._lock:
            self._master_level = level

    def set_singer_level(self, level):
        """Set the Singer Dimmer level. Queued under freeze when 'singer_dimmer'
        is in freeze_includes, instead of breaking through."""
        level = max(0.0, min(1.0, float(level)))
        if self._is_frozen_for("singer_dimmer"):
            with self._lock:
                self._pending_singer_level = level
            return
        with self._lock:
            self._singer_level = level

    def set_singer_mode(self, enabled):
        """Start a smooth crossfade to/from singer override. When freeze is
        active and 'singer_mode' is in freeze_includes, the toggle is queued
        and applied on unfreeze instead of breaking through the freeze."""
        enabled = bool(enabled)
        if self._is_frozen_for("singer_mode"):
            with self._lock:
                self._pending_singer = enabled
            return
        with self._lock:
            self._singer_mode   = enabled
            self._singer_target = 1.0 if enabled else 0.0

    def clear_all(self):
        """Panic reset to a clean slate. Stops every playing layer (cycler,
        main scenes, motions, looks, effects, overlay), clears blackout, turns
        singer mode OFF, and resets the master and singer dimmers to 100%.
        Bypasses freeze — a panic clear always applies immediately."""
        # Drop freeze (and any queued changes) so the stops/sets all go live.
        with self._lock:
            self._freeze_active = False
            self._pending_main_ids      = []
            self._pending_main_scenes   = {}
            self._pending_main_fades    = {}
            self._pending_motion_ids    = []
            self._pending_motion_scenes = {}
            self._pending_look_ids      = []
            self._pending_look_scenes   = {}
            self._pending_effect_ids    = []
            self._pending_effect_scenes = {}
            self._pending_singer        = None
            self._pending_master        = None
            self._pending_singer_level  = None
        # Stop every playing layer. Cycler first so it can't re-seed its decks
        # into the scene stack between stopping scenes and stopping the thread.
        self._stop_cycler_thread()
        self.stop_scene()                                    # graceful fade-out, all mains (incl. cycler decks)
        self._stop_all_mover_entries(self._active_motions)   # motion snaps
        self._stop_all_mover_entries(self._active_looks)     # look snaps
        self.stop_effect_scene()                             # graceful fade-out, all effects
        self.stop_overlay()                                  # graceful fade-out
        # Clear blackout (fade back to the now-empty scene).
        with self._lock:
            self._blackout_target = 0.0
        # Reset controls to a clean slate.
        self.set_singer_mode(False)   # singer override (warm white) OFF
        self.set_master(1.0)          # master / colour dimmer → 100%
        self.set_singer_level(1.0)    # singer dimmer → 100%
        log.info("CLEAR ALL — all layers stopped; controls reset to clean slate")

    def blackout(self, mode='full'):
        """
        Toggle blackout mode. Scene playback is NOT stopped — the blackout is
        applied as a faded filter on top of the running scene so disabling it
        instantly restores whatever was playing.

        - Calling with the currently-active mode: toggles it off (fade out)
        - Calling with a different mode while a blackout is active: switch modes
        - Calling with any mode while normal: activate that blackout (fade in)
        """
        if mode not in ('color', 'full'):
            mode = 'full'
        with self._lock:
            if self._blackout_mode == mode and self._blackout_target == 1.0:
                # Toggle off — fade back to scene
                self._blackout_target = 0.0
            else:
                # Activate (or switch to) this mode
                self._blackout_mode   = mode
                self._blackout_target = 1.0

    def set_blackout(self, mode):
        """Set blackout to a specific target (idempotent — no toggle), for
        preset recall. mode: None/'off' clears it; 'color' or 'full' applies."""
        if mode in (None, "off", "none", ""):
            with self._lock:
                self._blackout_target = 0.0
            return
        if mode not in ("color", "full"):
            mode = "full"
        with self._lock:
            self._blackout_mode   = mode
            self._blackout_target = 1.0

    # ── Overlay scene (e.g. strobe layer) ──────────────────────────────────

    def start_overlay(self, scene):
        """Start playing an overlay scene on top of the main scene.
        Smoothly fades the overlay in. The overlay scene loops continuously
        until stop_overlay() is called. Idempotent — calling again while
        active just keeps it on (does not restart the loop)."""
        with self._lock:
            already_on = (self._overlay_target == 1.0
                          and self._overlay_thread is not None
                          and self._overlay_thread.is_alive())
        if already_on:
            return
        self._stop_overlay_thread()
        self._overlay_stop_event.clear()
        with self._lock:
            self._current_overlay_name = scene.get("name")
            self._overlay_target       = 1.0
        self._overlay_thread = threading.Thread(
            target=self._overlay_loop,
            args=(scene,),
            daemon=True,
            name="overlay-player",
        )
        self._overlay_thread.start()

    def stop_overlay(self):
        """Fade the overlay out. The output-loop tick will stop the thread
        and clear state once the blend reaches zero."""
        with self._lock:
            self._overlay_target = 0.0

    def toggle_overlay(self, scene):
        """If overlay is on, fade it out. Otherwise start it with the given scene."""
        with self._lock:
            currently_on = (self._overlay_target == 1.0)
        if currently_on:
            self.stop_overlay()
        else:
            self.start_overlay(scene)

    def _stop_overlay_thread(self):
        self._overlay_stop_event.set()
        if self._overlay_thread and self._overlay_thread.is_alive():
            self._overlay_thread.join(timeout=1.0)
        self._overlay_thread = None

    def _overlay_loop(self, scene):
        """Loops the overlay scene continuously, writing to _overlay_dmx."""
        steps = scene.get("steps", [])
        if not steps:
            return
        while not self._overlay_stop_event.is_set():
            for step in steps:
                if self._overlay_stop_event.is_set():
                    return
                target  = self.resolve_step(step.get("fixtures", {}))
                fade_ms = max(0, int(step.get("fade", 0)))
                hold_ms = max(0, int(step.get("hold", 50)))
                self._execute_overlay_step(target, fade_ms, hold_ms)

    def _execute_overlay_step(self, target_dmx, fade_ms, hold_ms):
        """Same shape as _execute_step but writes to _overlay_dmx and
        watches _overlay_stop_event instead of _scene_stop_event."""
        with self._lock:
            start_dmx = dict(self._overlay_dmx)

        for ch in target_dmx:
            if ch not in start_dmx:
                start_dmx[ch] = 0

        if fade_ms > 0:
            t0       = time.time()
            duration = fade_ms / 1000.0
            while not self._overlay_stop_event.is_set():
                elapsed = time.time() - t0
                t       = min(elapsed / duration, 1.0)
                frame   = {}
                for ch, tgt in target_dmx.items():
                    frame[ch] = int(start_dmx.get(ch, 0) + (tgt - start_dmx.get(ch, 0)) * t)
                with self._lock:
                    self._overlay_dmx = frame
                if t >= 1.0:
                    break
                time.sleep(0.02)
        else:
            with self._lock:
                self._overlay_dmx = dict(target_dmx)

        if hold_ms > 0:
            self._overlay_stop_event.wait(timeout=hold_ms / 1000.0)

    # ── Mover layer playback (Motion + Look), Task B: stacked ──────────────
    # Each play adds an entry running its own player thread that writes to its
    # own entry["dmx"]. The layer composite (in _push_to_dmx) overlays entries
    # in play order. No layer-level fade (snap); step-internal fades still run.
    # play_*  = additive "ensure on" (re-playing an id restarts it in place);
    # stop_*  = remove one id, or all when scene_id is None;
    # toggle_* = stop if active else play (freeze-aware).

    def _start_mover_entry(self, stack, kind, scene, scene_id):
        entry = {
            "id":         scene_id,
            "name":       scene.get("name"),
            "scene":      scene,
            "dmx":        {},
            "thread":     None,
            "stop_event": threading.Event(),
        }
        with self._lock:
            stack.append(entry)
        entry["thread"] = threading.Thread(
            target=self._mover_loop, args=(entry, kind), daemon=True,
            name=f"{kind}-player",
        )
        entry["thread"].start()
        return entry

    def _stop_mover_entry(self, stack, scene_id):
        with self._lock:
            target = next((e for e in stack if e["id"] == scene_id), None)
            if target is None:
                return
            stack.remove(target)
        target["stop_event"].set()
        if target["thread"] and target["thread"].is_alive():
            target["thread"].join(timeout=0.5)

    def _stop_all_mover_entries(self, stack):
        with self._lock:
            entries = list(stack)
            stack.clear()
        for e in entries:
            e["stop_event"].set()
            if e["thread"] and e["thread"].is_alive():
                e["thread"].join(timeout=0.5)

    def play_motion_scene(self, scene, scene_id=None):
        """Add (ensure-on) a mover_motion scene (pan/tilt). Re-playing an id
        already in the stack restarts it in place. Queued under freeze."""
        if self._is_frozen_for("motion"):
            with self._lock:
                if scene_id is not None and scene_id not in self._pending_motion_ids:
                    self._pending_motion_ids.append(scene_id)
                    self._pending_motion_scenes[scene_id] = scene
            return
        if scene_id is not None:
            with self._lock:
                exists = any(e["id"] == scene_id for e in self._active_motions)
            if exists:
                self._stop_mover_entry(self._active_motions, scene_id)
        self._start_mover_entry(self._active_motions, "motion", scene, scene_id)

    def stop_motion_scene(self, scene_id=None):
        """Stop one mover_motion entry (scene_id) or all (None). Queued under
        freeze: drop a specific id from pending, or clear pending when None."""
        if self._is_frozen_for("motion"):
            with self._lock:
                if scene_id is None:
                    self._pending_motion_ids = []
                elif scene_id in self._pending_motion_ids:
                    self._pending_motion_ids.remove(scene_id)
            return
        if scene_id is None:
            self._stop_all_mover_entries(self._active_motions)
        else:
            self._stop_mover_entry(self._active_motions, scene_id)

    def toggle_motion_scene(self, scene, scene_id=None):
        """Tap-to-toggle: stop this id if active, else play it. Freeze-aware."""
        if self._is_frozen_for("motion"):
            with self._lock:
                if scene_id in self._pending_motion_ids:
                    self._pending_motion_ids.remove(scene_id)
                else:
                    self._pending_motion_ids.append(scene_id)
                    self._pending_motion_scenes[scene_id] = scene
            return
        with self._lock:
            on = any(e["id"] == scene_id for e in self._active_motions)
        if on:
            self.stop_motion_scene(scene_id)
        else:
            self.play_motion_scene(scene, scene_id=scene_id)

    def play_look_scene(self, scene, scene_id=None):
        """Add (ensure-on) a mover_look scene (dimmer/color/gobo/etc.). Queued
        under freeze."""
        if self._is_frozen_for("look"):
            with self._lock:
                if scene_id is not None and scene_id not in self._pending_look_ids:
                    self._pending_look_ids.append(scene_id)
                    self._pending_look_scenes[scene_id] = scene
            return
        if scene_id is not None:
            with self._lock:
                exists = any(e["id"] == scene_id for e in self._active_looks)
            if exists:
                self._stop_mover_entry(self._active_looks, scene_id)
        self._start_mover_entry(self._active_looks, "look", scene, scene_id)

    def stop_look_scene(self, scene_id=None):
        """Stop one mover_look entry (scene_id) or all (None). Queued under
        freeze like motion."""
        if self._is_frozen_for("look"):
            with self._lock:
                if scene_id is None:
                    self._pending_look_ids = []
                elif scene_id in self._pending_look_ids:
                    self._pending_look_ids.remove(scene_id)
            return
        if scene_id is None:
            self._stop_all_mover_entries(self._active_looks)
        else:
            self._stop_mover_entry(self._active_looks, scene_id)

    def toggle_look_scene(self, scene, scene_id=None):
        """Tap-to-toggle for looks. Freeze-aware."""
        if self._is_frozen_for("look"):
            with self._lock:
                if scene_id in self._pending_look_ids:
                    self._pending_look_ids.remove(scene_id)
                else:
                    self._pending_look_ids.append(scene_id)
                    self._pending_look_scenes[scene_id] = scene
            return
        with self._lock:
            on = any(e["id"] == scene_id for e in self._active_looks)
        if on:
            self.stop_look_scene(scene_id)
        else:
            self.play_look_scene(scene, scene_id=scene_id)

    def _mover_loop(self, entry, kind):
        """kind: 'motion' or 'look'. Loops the entry's scene continuously,
        writing to entry['dmx'] until the entry's stop_event is set."""
        scene = entry["scene"]
        # Procedural motion: a generator-mode mover_motion scene evaluates a
        # continuous shape against the beat clock each tick instead of stepping
        # between fixed waypoints. It publishes through the same entry['dmx']
        # surface, so freeze / preview / presets / compositing are unchanged.
        if kind == "motion" and scene.get("motion_mode") == "generator":
            self._generator_loop(entry)
            return
        scene_type = "mover_motion" if kind == "motion" else "mover_look"
        stop_event = entry["stop_event"]
        steps = scene.get("steps", [])
        if not steps:
            return
        while not stop_event.is_set():
            for step in steps:
                if stop_event.is_set():
                    return
                target  = self.resolve_step(step.get("fixtures", {}), scene_type=scene_type)
                fade_ms = max(0, int(step.get("fade", 0)))
                hold_ms = max(0, int(step.get("hold", 500)))
                self._execute_mover_step(entry, target, fade_ms, hold_ms)

    def _generator_loop(self, entry):
        """Continuous procedural mover motion. Evaluates the scene's shape once
        per output tick and writes the resulting pan/tilt frame to entry['dmx'].

        Speed follows the same musical/wall-clock split the effect renderer
        uses: when the scene is tempo-synced and a tap tempo is live, t_eff is
        beats-since-anchor / beat_division (1 cycle per `division` beats);
        otherwise it's wall-clock seconds * the generator's `speed` (cycles/s).
        """
        scene = entry["scene"]
        stop_event = entry["stop_event"]
        gen = scene.get("generator") or {}
        try:
            speed = float(gen.get("speed", 0.25))
        except (TypeError, ValueError):
            speed = 0.25
        interval = 1.0 / self.OUTPUT_HZ
        t0 = time.time()
        while not stop_event.is_set():
            if scene.get("tempo_sync") and self._tempo_active:
                division = float(scene.get("beat_division", 1.0)) or 1.0
                t_eff = self.beat_time() / division
            else:
                t_eff = (time.time() - t0) * speed
            role_frame = mover_gen.evaluate_motion_generator(scene, t_eff)
            target = self.resolve_step(role_frame, scene_type="mover_motion")
            with self._lock:
                entry["dmx"] = target
            stop_event.wait(timeout=interval)

    def _execute_mover_step(self, entry, target_dmx, fade_ms, hold_ms):
        """Fade-and-hold for one mover entry's step. Writes to entry['dmx']."""
        stop_event = entry["stop_event"]
        with self._lock:
            start_dmx = dict(entry["dmx"])
        for ch in target_dmx:
            if ch not in start_dmx:
                start_dmx[ch] = 0

        if fade_ms > 0:
            t0       = time.time()
            duration = fade_ms / 1000.0
            while not stop_event.is_set():
                elapsed = time.time() - t0
                t       = min(elapsed / duration, 1.0)
                frame   = {}
                for ch, tgt in target_dmx.items():
                    frame[ch] = int(start_dmx.get(ch, 0) + (tgt - start_dmx.get(ch, 0)) * t)
                with self._lock:
                    entry["dmx"] = frame
                if t >= 1.0:
                    break
                time.sleep(0.02)
        else:
            with self._lock:
                entry["dmx"] = dict(target_dmx)

        stop_event.wait(timeout=hold_ms / 1000.0)

    # ── Tempo / tap-tempo clock ────────────────────────────────────────────
    # One global beat grid that scene step-sync, the effect renderer, and the
    # beat cycler all read. Pure math from (_bpm, _beat_anchor); no thread.
    # Taps accumulate in a buffer and only commit to the live clock once tapping
    # settles, so the show never retimes mid-burst. _tempo_active is the master
    # gate: when False, every synced consumer uses its own default.

    def _estimate_bpm_locked(self):
        """Median-filtered BPM from the tap buffer. Call under lock. 0.0 if <2 taps."""
        ts = self._tap_times
        if len(ts) < 2:
            return 0.0
        intervals = [b - a for a, b in zip(ts, ts[1:])]
        s = sorted(intervals)
        mid = s[len(s) // 2]
        good = [iv for iv in intervals if 0.5 * mid <= iv <= 2.0 * mid] or intervals
        avg = sum(good) / len(good)
        if avg <= 0:
            return 0.0
        return max(self._bpm_min, min(self._bpm_max, 60.0 / avg))

    def tap(self):
        """Register one tap. Updates only the buffer + live preview estimate;
        the live clock is untouched until the burst settles. Returns status."""
        now = time.time()
        with self._lock:
            if self._tap_times and (now - self._tap_times[-1]) > self._tap_reset_s:
                self._tap_times = []          # gap too long -> new series
            self._tap_times.append(now)
            if len(self._tap_times) > 8:
                self._tap_times = self._tap_times[-8:]
            self._tap_pending     = True
            self._tap_preview_bpm = self._estimate_bpm_locked()
        return self.tempo_status()

    def _tick_tempo_commit(self):
        """Commit a settled tap burst into the live clock. Runs every output
        tick. The "have you stopped tapping?" window scales with how fast you
        are tapping -- it waits ~1.5x your current inter-tap interval (floored
        by _tap_settle_s, capped by _tap_reset_s) before deciding the burst is
        over. A fixed window can't serve both fast and slow tapping: at slow
        tempos a short fixed settle ends the burst between taps, dropping the
        live preview and never reaching _tap_min."""
        with self._lock:
            if not self._tap_pending or not self._tap_times:
                return
            idle = time.time() - self._tap_times[-1]
            # Adaptive settle: longer when tapping slowly, so the burst (and the
            # "tapping…" preview) survives the gap between taps.
            settle = self._tap_settle_s
            if self._tap_preview_bpm > 0:
                interval = 60.0 / self._tap_preview_bpm
                settle = max(self._tap_settle_s,
                             min(self._tap_reset_s, interval * 1.5))
            if idle < settle:
                return                        # still mid-burst; wait for more taps
            if len(self._tap_times) >= self._tap_min:
                bpm = self._estimate_bpm_locked()
                if bpm > 0:
                    self._bpm          = bpm
                    self._beat_anchor  = self._tap_times[-1]   # downbeat on last tap
                    self._tempo_active = True
                self._tap_times       = []
                self._tap_pending     = False
                self._tap_preview_bpm = 0.0
            elif idle > self._tap_reset_s:
                # Too few taps and the user has clearly stopped -> abandon buffer.
                self._tap_times       = []
                self._tap_pending     = False
                self._tap_preview_bpm = 0.0
            # else: 1-2 taps but still within the reset window -> keep collecting.

    def tempo_cancel(self):
        """Zero out tap tempo. _tempo_active=False makes every synced scene/
        effect revert to its own default (ms hold/fade, Hz speed) on its next
        boundary; the cycler stops too. Also clears any in-progress buffer."""
        with self._lock:
            self._tempo_active    = False
            self._bpm             = 0.0
            self._beat_anchor     = 0.0
            self._tap_times       = []
            self._tap_pending     = False
            self._tap_preview_bpm = 0.0
        self._stop_cycler_thread()   # cancel stops the cycler as well
        return self.tempo_status()

    def tempo_nudge(self, delta_bpm):
        """Trim committed BPM by delta (+-1 etc.) without re-tapping, preserving
        the current beat phase so the grid doesn't jump. No-op if inactive."""
        with self._lock:
            if not self._tempo_active or self._bpm <= 0:
                return self.tempo_status()
            now    = time.time()
            period = 60.0 / self._bpm
            n      = int((now - self._beat_anchor) / period)   # floor (>=0)
            last_beat = self._beat_anchor + n * period
            self._bpm         = max(self._bpm_min,
                                    min(self._bpm_max, self._bpm + delta_bpm))
            self._beat_anchor = last_beat
        return self.tempo_status()

    def tempo_resync(self):
        """Drop a downbeat NOW without changing BPM -- re-phases the grid onto
        the music once the tempo's already right."""
        with self._lock:
            if self._tempo_active:
                self._beat_anchor = time.time()
        return self.tempo_status()

    # Lock-free reads: only touch a few floats and never re-acquire the lock,
    # so they're safe from inside an already-locked section (get_state) AND from
    # the unlocked effect-render path in _push_to_dmx.

    def beat_time(self, now=None):
        """The 'musical t': beats elapsed since the anchor. The effect renderer
        feeds this in place of wall-clock seconds when a scene is tempo-synced,
        so speed=1.0 means one cycle per beat. 0.0 when tempo is inactive."""
        if not self._tempo_active or self._bpm <= 0:
            return 0.0
        if now is None:
            now = time.time()
        return (now - self._beat_anchor) * (self._bpm / 60.0)

    def beat_phase(self, now=None):
        """Position within the current beat, 0.0..1.0 -- for a pulsing UI dot."""
        if not self._tempo_active or self._bpm <= 0:
            return 0.0
        if now is None:
            now = time.time()
        period = 60.0 / self._bpm
        return ((now - self._beat_anchor) % period) / period

    def next_beat_boundary(self, after, division=1.0):
        """Wall-clock time of the next grid boundary strictly after `after`.
        `division` = beats per step (0.5 = half-beat, 2 = every two beats).
        Returns None when tempo is inactive so the scene loop falls back to ms."""
        if not self._tempo_active or self._bpm <= 0:
            return None
        step = (60.0 / self._bpm) * max(0.01, float(division))
        k = int((after - self._beat_anchor) / step) + 1
        return self._beat_anchor + k * step

    def tempo_status(self):
        """Snapshot for routes + UI. Safe to call without holding the lock."""
        now = time.time()
        with self._lock:
            active = self._tempo_active
            bpm    = self._bpm
            pend   = self._tap_pending
            prev   = self._tap_preview_bpm
            ntaps  = len(self._tap_times)
        return {
            "active":      active,
            "bpm":         round(bpm, 1) if bpm else 0.0,
            "beat_phase":  round(self.beat_phase(now), 3),
            "tapping":     pend,
            "preview_bpm": round(prev, 1) if prev else 0.0,
            "tap_count":   ntaps,
        }

    # ── Beat cycler ────────────────────────────────────────────────────────
    # Chases a chosen set of existing scenes (looks), advancing one per
    # beat-division off the same clock. Plays looks on two reserved decks
    # through the normal scene stack so crossfade/singer/master all apply.

    CYCLER_DECK_A = "__cycler_a__"
    CYCLER_DECK_B = "__cycler_b__"

    def start_cycler(self, scenes, division=1.0, crossfade_ms=None):
        """Arm the cycler with a list of scene dicts; advance one look per
        `division` beats. If no tempo is live yet it waits (armed) until one is
        committed. Re-calling replaces the look set / division live."""
        scenes = [s for s in (scenes or []) if s]
        if not scenes:
            return self.cycler_status()
        with self._lock:
            self._cycler_scenes   = scenes
            self._cycler_division = max(0.01, float(division))
            if crossfade_ms is not None:
                self._cycler_xfade_ms = max(0, int(crossfade_ms))
            if self._cyc_index >= len(scenes):
                self._cyc_index = 0
            self._cycler_active = True
        if self._cycler_thread is None or not self._cycler_thread.is_alive():
            self._cycler_stop_event = threading.Event()
            self._cyc_started = False
            self._cycler_thread = threading.Thread(
                target=self._cycler_loop, daemon=True, name="beat-cycler")
            self._cycler_thread.start()
        return self.cycler_status()

    def stop_cycler(self):
        """Stop the cycler and fade out its decks."""
        self._stop_cycler_thread()
        return self.cycler_status()

    def _stop_cycler_thread(self):
        with self._lock:
            self._cycler_active = False
        if self._cycler_thread is not None:
            self._cycler_stop_event.set()
            if self._cycler_thread.is_alive():
                self._cycler_thread.join(timeout=0.5)
            self._cycler_thread = None
        # Fade both decks out (low-level path bypasses the freeze gate)
        self._stop_scene_by_id(self.CYCLER_DECK_A, immediate=False)
        self._stop_scene_by_id(self.CYCLER_DECK_B, immediate=False)
        with self._lock:
            self._cyc_deck    = None
            self._cyc_started = False

    def _cycler_advance(self, slot_s):
        """Crossfade the next look in on the free deck and fade the current deck
        out. Crossfade is capped so it can't overrun the beat slot."""
        with self._lock:
            scenes = list(self._cycler_scenes)
            idx    = self._cyc_index
            deck   = self._cyc_deck
            xfade  = self._cycler_xfade_ms
        if not scenes:
            return
        nxt_idx  = 0 if deck is None else (idx + 1) % len(scenes)
        nxt_deck = self.CYCLER_DECK_B if deck == self.CYCLER_DECK_A else self.CYCLER_DECK_A
        xfade_ms = int(min(xfade, max(0.0, slot_s * 0.9) * 1000))
        self.play_scene(scenes[nxt_idx], launch_fade_ms=xfade_ms,
                        scene_id=nxt_deck, force=True)
        if deck is not None:
            self._stop_scene_by_id(deck, immediate=False)   # graceful fade out
        with self._lock:
            self._cyc_index = nxt_idx
            self._cyc_deck  = nxt_deck

    def _cycler_loop(self):
        """~20Hz driver. Edge-detects beat-grid crossings so it retimes cleanly
        on a re-tap and holds (armed) when no tempo is live."""
        ev = self._cycler_stop_event
        while not ev.is_set():
            now = time.time()
            with self._lock:
                active   = self._cycler_active
                division = self._cycler_division
                started  = self._cyc_started
                last_idx = self._cyc_last_idx
                last_clk = self._cyc_last_clock
            if not active:
                break
            if not (self._tempo_active and self._bpm > 0):
                if started:                       # tempo went away -- re-arm
                    with self._lock:
                        self._cyc_started = False
                ev.wait(0.05)
                continue
            bpm    = self._bpm
            anchor = self._beat_anchor
            slot_s = (60.0 / bpm) * division
            idx    = int((now - anchor) / slot_s) if slot_s > 0 else 0
            clk    = (bpm, anchor)
            if not started:
                self._cycler_advance(slot_s)            # first look immediately
                with self._lock:
                    self._cyc_started    = True
                    self._cyc_last_idx   = idx
                    self._cyc_last_clock = clk
            elif clk != last_clk:
                with self._lock:                        # re-tap -> silent re-base
                    self._cyc_last_idx   = idx
                    self._cyc_last_clock = clk
            elif idx > last_idx:
                self._cycler_advance(slot_s)            # crossed into a new slot
                with self._lock:
                    self._cyc_last_idx = idx
            ev.wait(0.02)

    def cycler_status(self):
        """Snapshot for routes + UI. Safe without holding the lock."""
        with self._lock:
            active = self._cycler_active
            n      = len(self._cycler_scenes)
            idx    = self._cyc_index
            div    = self._cycler_division
            name   = (self._cycler_scenes[idx].get("name")
                      if active and n and idx < n else None)
        return {"active": active, "count": n,
                "index": idx if active and n else None,
                "name": name, "division": div}

    def get_state(self):
        with self._lock:
            actives = [
                {"id": s["id"], "name": s["name"], "stopping": s["stopping"]}
                for s in self._active_scenes
                if not (s["id"] or "").startswith("__cycler")
            ]
            # Stacked layers (Task B). Each list is bottom-first (play order),
            # which is the order presets capture and re-apply.
            motions = [{"id": e["id"], "name": e["name"]} for e in self._active_motions]
            looks   = [{"id": e["id"], "name": e["name"]} for e in self._active_looks]
            effects = [{"id": e["id"], "name": e["name"], "stopping": e["target"] == 0.0}
                       for e in self._active_effects]
            # Topmost-active (non-stopping) per layer for legacy single fields.
            top_motion = motions[-1] if motions else None
            top_look   = looks[-1] if looks else None
            top_effect = next((e for e in reversed(effects) if not e["stopping"]), None)
            max_effect_blend = max((e["blend"] for e in self._active_effects), default=0.0)
            freeze = {
                "active": self._freeze_active,
                "pending_main_ids":   list(self._pending_main_ids),
                "pending_motion_ids": list(self._pending_motion_ids),
                "pending_look_ids":   list(self._pending_look_ids),
                "pending_effect_ids": list(self._pending_effect_ids),
                "pending_singer": self._pending_singer,
                "pending_master": self._pending_master,
                "pending_singer_level": self._pending_singer_level,
            }
            return {
                # Multi-scene state: list of currently active main scenes
                "scenes":            actives,
                # Stacked motion / look / effect (Task B)
                "motions":           motions,
                "looks":             looks,
                "effects":           effects,
                # Backward-compat single-scene fields (uses most recent active)
                "current_scene":     actives[-1]["name"] if actives else None,
                "current_scene_id":  actives[-1]["id"]   if actives else None,
                "master_level":      self._master_level,
                "singer_mode":       self._singer_mode,
                "singer_level":      self._singer_level,
                "singer_blend":      round(self._singer_blend, 3),
                "blackout_mode":     self._blackout_mode,
                "blackout_blend":    round(self._blackout_blend, 3),
                "overlay_active":    self._overlay_target == 1.0,
                "overlay_blend":     round(self._overlay_blend, 3),
                "overlay_name":      self._current_overlay_name,
                # Legacy single fields = topmost active of each stack (deprecated;
                # prefer the motions/looks/effects lists above).
                "current_motion":    top_motion["name"] if top_motion else None,
                "current_motion_id": top_motion["id"]   if top_motion else None,
                "current_look":      top_look["name"] if top_look else None,
                "current_look_id":   top_look["id"]   if top_look else None,
                "current_effect":    top_effect["name"] if top_effect else None,
                "current_effect_id": top_effect["id"]   if top_effect else None,
                "effect_blend":      round(max_effect_blend, 3),
                "dmx_connected":     self._dmx.connected,
                "freeze":            freeze,
                "tempo": {
                    "active":      self._tempo_active,
                    "bpm":         round(self._bpm, 1) if self._bpm else 0.0,
                    "beat_phase":  round(self.beat_phase(), 3),
                    "tapping":     self._tap_pending,
                    "preview_bpm": round(self._tap_preview_bpm, 1)
                                   if self._tap_preview_bpm else 0.0,
                    "tap_count":   len(self._tap_times),
                },
                "cycler": {
                    "active":   self._cycler_active,
                    "count":    len(self._cycler_scenes),
                    "index":    self._cyc_index
                                if (self._cycler_active and self._cycler_scenes) else None,
                    "name":     (self._cycler_scenes[self._cyc_index].get("name")
                                 if self._cycler_active and self._cycler_scenes
                                 and self._cyc_index < len(self._cycler_scenes) else None),
                    "division": self._cycler_division,
                },
            }

    def shutdown(self):
        self._output_running = False
        self.stop_all_scenes()
        self._stop_overlay_thread()
        self._stop_all_mover_entries(self._active_motions)
        self._stop_all_mover_entries(self._active_looks)
        self._stop_cycler_thread()
        # Effect layer has no threads of its own — just clear state.
        with self._lock:
            self._active_effects        = []
            self._preview_effect_scene  = None
            self._preview_effect_target = 0.0
            self._preview_effect_blend  = 0.0
            self._effect_dmx            = {}

    def set_dmx(self, new_dmx):
        """Hot-swap the DMX output driver. Returns the previous driver so the
        caller can disconnect it. The output loop keeps running uninterrupted."""
        with self._lock:
            old = self._dmx
            self._dmx = new_dmx
        return old

    # ── Live preview from editor ───────────────────────────────────────────

    PREVIEW_TAG = "__preview__"

    def preview_set(self, scene_type, step_fixtures, lit=None):
        """Write a live preview frame to the appropriate DMX slot for the
        scene type being edited. For main scenes, the preview takes over the
        composite output entirely (active scenes keep running but their values
        are hidden until preview clears). For motion/look, the preview OVERRIDES
        that layer's composited stack (the live stack keeps running underneath
        and resumes on clear).

        scene_type: 'main' | 'mover_motion' | 'mover_look'
        lit: optional iterable of mover ids that should be lit (dimmer=255)
             during a motion preview. When None, every mover present in the
             frame is auto-lit (legacy behavior, used by the step editor). When
             provided, listed movers are lit and the rest are forced dark
             (dimmer=0) so a single fixture can be aimed in isolation.
        """
        target = self.resolve_step(step_fixtures or {}, scene_type=scene_type)

        # For motion previews, drive the dimmer so the user actually sees the
        # beams move without needing a Look scene running. The motion_dmx layer
        # overrides the look layer, so this wins.
        if scene_type == "mover_motion":
            lit_set = None if lit is None else set(lit)
            for fx in self._show.get("fixtures", []):
                if not self._is_mover(fx):
                    continue
                if fx["id"] not in (step_fixtures or {}):
                    continue
                ch = self._mover_channel_for_role(fx, "dimmer")
                if ch is None:
                    continue
                if lit_set is None:
                    val = 255
                else:
                    val = 255 if fx["id"] in lit_set else 0
                target[(self._fx_universe(fx), ch)] = val

        if scene_type == "main":
            with self._lock:
                self._preview_dmx    = target
                self._preview_active = True
        elif scene_type == "mover_motion":
            with self._lock:
                self._preview_motion_dmx    = target
                self._preview_motion_active = True
        elif scene_type == "mover_look":
            with self._lock:
                self._preview_look_dmx    = target
                self._preview_look_active = True

    def preview_clear(self, scene_type):
        """Clear the preview in the given slot, returning that slot to empty
        so the live stack composite shows through again."""
        if scene_type == "main":
            with self._lock:
                self._preview_active = False
                self._preview_dmx    = {}
        elif scene_type == "mover_motion":
            with self._lock:
                self._preview_motion_active = False
                self._preview_motion_dmx    = {}
        elif scene_type == "mover_look":
            with self._lock:
                self._preview_look_active = False
                self._preview_look_dmx    = {}

    # ── Raw channel test (fixture builder discovery probe) ────────────────
    #
    # Drives arbitrary DMX addresses directly, with no fixture defined, so an
    # unknown fixture's channel layout can be discovered by sweeping faders and
    # watching the light. raw_set() replaces the whole override each call (the
    # UI posts its full fader state), so releasing a fader to 0 keeps it at 0.
    # Always release with raw_clear() when finished.

    def raw_set(self, channels, solo=None):
        ov = {}
        for uni, ch, val in channels:
            uni = int(uni); ch = int(ch)
            if 1 <= ch <= 512:
                ov[(uni, ch)] = max(0, min(255, int(val)))
        with self._lock:
            # Addresses we were driving but no longer are, owned by no fixture,
            # must be actively zeroed so they don't latch in the Art-Net buffer.
            dropped = (set(self._raw_override) - set(ov)) - self._patched_channels_set
            if dropped:
                self._raw_release_keys |= dropped
                self._raw_release_count = max(self._raw_release_count, 4)
            self._raw_release_keys -= set(ov)
            self._raw_override = ov
            self._raw_active   = True
            if solo is not None:
                self._raw_solo = bool(solo)

    def raw_clear(self):
        with self._lock:
            orphans = set(self._raw_override) - self._patched_channels_set
            if orphans:
                self._raw_release_keys |= orphans
                self._raw_release_count = 4
            self._raw_override = {}
            self._raw_active   = False
            self._raw_solo     = False

    def load_show(self, show_config):
        """Hot-swap to a new show config — recomputes channel maps."""
        with self._lock:
            self._show = show_config
            self._singer_fade_ms   = show_config.get("singer_fade_ms", 1500)
            self._blackout_fade_ms = show_config.get("blackout_fade_ms", 600)
            self._overlay_fade_ms  = show_config.get("overlay_fade_ms", 200)
            self._effect_fade_ms   = show_config.get("effect_fade_ms", 500)
            self._overlay_keep_singer = show_config.get("overlay_keep_singer", True)
            # Reload per-show freeze includes
            self._freeze_includes = self._load_freeze_includes(show_config)
        self.stop_all_scenes()
        self._stop_overlay_thread()
        self._stop_all_mover_entries(self._active_motions)
        self._stop_all_mover_entries(self._active_looks)
        self._stop_cycler_thread()
        with self._lock:
            self._scene_dmx            = {}
            self._preview_dmx          = {}
            self._preview_active       = False
            # Reset freeze state when changing shows
            self._freeze_active        = False
            self._pending_main_ids     = []
            self._pending_main_scenes  = {}
            self._pending_main_fades   = {}
            self._pending_motion_ids    = []
            self._pending_motion_scenes = {}
            self._pending_look_ids      = []
            self._pending_look_scenes   = {}
            self._pending_effect_ids    = []
            self._pending_effect_scenes = {}
            # Reset tempo / cycler state when changing shows
            self._tempo_active         = False
            self._bpm                  = 0.0
            self._beat_anchor          = 0.0
            self._tap_times            = []
            self._tap_pending          = False
            self._tap_preview_bpm      = 0.0
            self._cycler_active        = False
            self._cycler_scenes        = []
            self._cyc_index            = 0
            self._blackout_mode        = None
            self._blackout_blend       = 0.0
            self._blackout_target      = 0.0
            self._overlay_dmx          = {}
            self._overlay_blend        = 0.0
            self._overlay_target       = 0.0
            self._current_overlay_name = None
            # Mover stacks reset (threads stopped above)
            self._active_motions        = []
            self._preview_motion_dmx    = {}
            self._preview_motion_active = False
            self._active_looks          = []
            self._preview_look_dmx      = {}
            self._preview_look_active   = False
            # Effect stack reset (no threads to stop)
            self._active_effects        = []
            self._effect_dmx            = {}
            self._preview_effect_scene  = None
            self._preview_effect_blend  = 0.0
            self._preview_effect_target = 0.0
            self._dimmer_channels     = self._get_dimmer_channels()
            self._singer_channels     = self._get_singer_channels()
            self._singer_dmx_full     = self._build_singer_dmx()
            self._patched_channels    = self._get_all_patched_channels()
            self._cell_strips         = self._build_cell_strips_cache()
            self._rebuild_channel_caches()
        # Clear any leftover DMX output from previous show
        self._dmx.blackout()
        # Re-freeze the new show's static objects out of the GC scan set.
        self._tune_gc()
