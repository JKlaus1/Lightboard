"""Preset storage + recall logic for Lightboard (Task A).

A preset is a "recall bundle": which library items should be active in each
playback category (main / motion / look / effect), plus scalar control levels,
gated by a per-subsystem `scope` checklist and an additive/exclusive flag.

Ordering only matters *within* a category — the engine output pipeline fixes
precedence *between* categories — so `items` holds an ordered id list per
category, applied bottom-first (matching the live stacks).

This module is pure logic (no Flask, no engine import): `apply_preset` and
`capture_preset` take the engine + a scene-loader as arguments so they can be
unit-tested against a stub engine. app.py wires the HTTP routes.

Preset shape:
    { id, name,
      exclusive: bool,
      scope:  { main, motion, look, effect, master, singer_mode, singer_level },
      items:  { main:[id...], motion:[id...], look:[id...], effect:[id...] },
      levels: { master, singer_mode, singer_level } }
"""
import json
import secrets
from pathlib import Path

PRESETS_FILE = Path(__file__).parent / "presets.json"

CATEGORIES = ("main", "motion", "look", "effect")
SCALARS    = ("master", "singer_mode", "singer_level")
SUBSYSTEMS = ("blackout", "overlay", "cycler")   # start/stop side-effect modes


def new_preset_id():
    """Short URL-safe id, p-prefixed to distinguish from scene ids."""
    return "p" + (secrets.token_urlsafe(6).replace("_", "").replace("-", "")[:7] or "preset")


# ── Storage ────────────────────────────────────────────────────────────────

def load_presets():
    if not PRESETS_FILE.exists():
        return []
    try:
        data = json.loads(PRESETS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_presets(presets):
    PRESETS_FILE.write_text(json.dumps(presets, indent=2))


def find_preset(presets, preset_id):
    for p in presets:
        if p.get("id") == preset_id:
            return p
    return None


# ── Normalization ───────────────────────────────────────────────────────────

def _default_scope(value=True):
    d = {c: value for c in CATEGORIES}
    d.update({s: value for s in SCALARS})
    d.update({s: value for s in SUBSYSTEMS})
    return d


def normalize_preset(p):
    """Fill in any missing fields so partial / older presets apply safely."""
    p = dict(p or {})
    p.setdefault("id", new_preset_id())
    p.setdefault("name", "Preset")
    p["exclusive"] = bool(p.get("exclusive", True))
    scope = _default_scope(False)
    scope.update(p.get("scope") or {})
    p["scope"] = {k: bool(scope.get(k)) for k in (*CATEGORIES, *SCALARS, *SUBSYSTEMS)}
    items = p.get("items") or {}
    p["items"] = {c: list(items.get(c) or []) for c in CATEGORIES}
    p["levels"] = dict(p.get("levels") or {})
    return p


# ── Capture / apply ──────────────────────────────────────────────────────────

def _live_ids(state):
    return {
        "main":   [s["id"] for s in state.get("scenes", [])],
        "motion": [m["id"] for m in state.get("motions", [])],
        "look":   [l["id"] for l in state.get("looks", [])],
        "effect": [e["id"] for e in state.get("effects", []) if not e.get("stopping")],
    }


def capture_preset(engine, name="Captured"):
    """Snapshot the live rig into a new exclusive, fully-scoped preset."""
    state = engine.get_state()
    live = _live_ids(state)
    cyc = state.get("cycler") or {}
    return {
        "id":        new_preset_id(),
        "name":      name or "Preset",
        "exclusive": True,
        "scope":     _default_scope(True),
        "items":     {c: list(live[c]) for c in CATEGORIES},
        "levels": {
            "master":       state.get("master_level", 1.0),
            "singer_mode":  state.get("singer_mode", False),
            "singer_level": state.get("singer_level", 1.0),
            # Subsystems: blackout = None/'color'/'full'; overlay = bool;
            # cycler = beat-division when running, else None (= off).
            "blackout":     state.get("blackout_mode"),
            "overlay":      bool(state.get("overlay_active")),
            "cycler":       (cyc.get("division") if cyc.get("active") else None),
        },
    }


def apply_preset(preset, engine, scene_loader, overlay_scene=None, cycler_scenes=None):
    """Recall a preset onto the live engine.

    scene_loader(scene_id) -> scene dict (raises FileNotFoundError if missing).
    overlay_scene: the show's resolved overlay scene dict (or None) — needed to
        start the overlay when the preset scopes it on.
    cycler_scenes: the show's resolved cycler scene dicts (list) — needed to
        start the cycler when the preset scopes it on.

    For each SCOPED playback category:
      - exclusive: stop live items not in the preset, then play the preset's
        items that aren't already live, in list order;
      - additive: play the preset's items not already live, in order; stop nothing.
    Scalars (master / singer_mode / singer_level) and subsystems (blackout /
    overlay / cycler) are set only if scoped in. Returns {started, stopped,
    missing, notes}.
    """
    p = normalize_preset(preset)
    scope, items, exclusive, levels = p["scope"], p["items"], p["exclusive"], p["levels"]
    live = _live_ids(engine.get_state())

    play = {
        "main":   lambda sid, sc: engine.play_scene(sc, scene_id=sid),
        "motion": lambda sid, sc: engine.play_motion_scene(sc, scene_id=sid),
        "look":   lambda sid, sc: engine.play_look_scene(sc, scene_id=sid),
        "effect": lambda sid, sc: engine.play_effect_scene(sc, scene_id=sid),
    }
    stop = {
        "main":   lambda sid: engine.stop_scene(scene_id=sid),
        "motion": lambda sid: engine.stop_motion_scene(scene_id=sid),
        "look":   lambda sid: engine.stop_look_scene(scene_id=sid),
        "effect": lambda sid: engine.stop_effect_scene(scene_id=sid),
    }

    started, stopped, missing, notes = [], [], [], []
    for cat in CATEGORIES:
        if not scope.get(cat):
            continue                       # unscoped category is never touched
        desired  = items.get(cat, [])
        live_ids = live[cat]
        if exclusive:
            for sid in live_ids:
                if sid not in desired:
                    stop[cat](sid)
                    stopped.append([cat, sid])
        for sid in desired:
            if sid in live_ids:
                continue                   # already running — leave it (keeps its slot)
            try:
                sc = scene_loader(sid)
            except FileNotFoundError:
                missing.append([cat, sid])
                continue
            play[cat](sid, sc)
            started.append([cat, sid])

    # Scalars: set-if-scoped (exclusive is a no-op on a value).
    if scope.get("master") and levels.get("master") is not None:
        engine.set_master(float(levels["master"]))
    if scope.get("singer_mode") and levels.get("singer_mode") is not None:
        engine.set_singer_mode(bool(levels["singer_mode"]))
    if scope.get("singer_level") and levels.get("singer_level") is not None:
        engine.set_singer_level(float(levels["singer_level"]))

    # Subsystems: set-if-scoped.
    if scope.get("blackout"):
        engine.set_blackout(levels.get("blackout"))     # None/'color'/'full'
    if scope.get("overlay"):
        if levels.get("overlay"):
            if overlay_scene is not None:
                engine.start_overlay(overlay_scene)
            else:
                notes.append("overlay scoped on but no overlay scene configured")
        else:
            engine.stop_overlay()
    if scope.get("cycler"):
        cyc = levels.get("cycler")
        if cyc:                                          # truthy = on
            if cycler_scenes:
                # bool True -> default division 1.0; a number -> that division.
                div = float(cyc) if isinstance(cyc, (int, float)) and not isinstance(cyc, bool) else 1.0
                engine.start_cycler(cycler_scenes, division=div)
            else:
                notes.append("cycler scoped on but no cycler scenes configured")
        else:
            engine.stop_cycler()

    return {"started": started, "stopped": stopped, "missing": missing, "notes": notes}
