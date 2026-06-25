# Lightboard — Roadmap & Working Notes

## Current deployed baseline (this commit)
Live on the Pi (`pi@192.168.1.34`, `/home/pi/lightboard`, systemd unit `lightboard`):

- **Channel-test panel** in the fixture builder — raw DMX override layer in the
  engine (`_raw_override` / `raw_set` / `raw_clear`; orphan-release on stop),
  routes `/api/dmx/raw` and `/api/dmx/raw/clear`. Per-fader **+/- nudge** for
  exact values. Optional **Solo** blackout (default off).
- **Wheel-slot photo presets** (gobo / colour / prism) — `channel_slots` in the
  fixture def; 512px thumbnails under `static/slots/<fixture_id>/<role>/<uid>.jpg`;
  routes `/api/fixtures/<id>/slot-image` (POST/DELETE) + cleanup on fixture
  delete. Slots store a **lo/hi range**, output the **midpoint**. Capture from the
  test panel (mark low / mark high + nudge + photo).
- **Second gobo wheel + second prism** roles: `gobo2`, `gobo2_rot`, `prism2`,
  `prism2_rot`.
- **Look-editor thumbnail picker** (Phase 2): mover show-instances carry
  `def_id` + `channel_slots`; picker pulls current library slots fresh.

## Roadmap (agreed order: GitHub → B → A) — ✅ ALL DONE

All three roadmap phases are implemented and deployed. New work below this line.

### 1. GitHub migration ✅ DONE
Repo is the source of truth. Pi **pulls only** (public repo, no auth). Pushes
happen from **Termux** (phone) where GitHub auth already works.
Steady-state deploy: edit the `~/Lightboard` clone → commit → push →
on Pi `git pull && sudo systemctl restart lightboard`. **No direct Pi patching anymore.**

### 2. B — Concurrency engine upgrade ✅ DONE (commit 40e86c6)
Motion / look / effect are now **composited stacks** like main scenes, so
multiple run at once (different motions/looks on different movers, layered
effects). Key outcomes the rest of the roadmap relies on:
- Engine stacks: `_active_motions`, `_active_looks`, `_active_effects`. Motion/
  look = one player thread per entry, snap (no layer fade), composited in play
  order. Effects = per-entry blend, rendered+composited per tick.
- Engine API per layer: `play_*` (additive ensure-on), `stop_*(scene_id=None)`
  (one or all), `toggle_*` (freeze-aware tap-to-toggle). Routes:
  `/api/<layer>/<id>` toggles; `/api/<layer>/stop` clears the layer;
  `/api/<layer>/stop/<id>` stops one.
- `get_state()` exposes ordered (bottom-first = play order) lists `motions`,
  `looks`, `effects`; freeze block exposes `pending_<layer>_ids`. Legacy
  `current_<layer>_id` now = topmost active (deprecated; presets use the lists).
- Singer-pod "below" fold composites all effects in order; reduces exactly to
  the old single-effect math at n=1 (regression-tested in `test_engine_2f.py`).
- Previews use dedicated slots that override (motion/look) / suppress (effect)
  the live stack, kept out of the stack and the freeze queue.
- Coverage: `test_engine_concurrency.py` (stacking, singer-fold isolation,
  freeze→unfreeze diff across all three stacks).

### 3. A — Presets ✅ DONE + category clear buttons ✅ DONE

**Clear buttons** (commit d262e0e; always-visible tweak after):
- Per-category **✕ Clear** in each card header (main / motion / look / effect),
  always visible, hitting that layer's stop-all route (`/api/stop`,
  `/api/<layer>/stop`). Motion/look only render when the show has movers.
- **✕ Clear All** in the now-playing row → `/api/clear-all` (`engine.clear_all()`):
  stops cycler + all scenes/motion/look/effect + overlay, clears blackout, turns
  singer mode OFF, and resets Color Dimmer + Singer Dimmer to 100%. Bypasses
  freeze. Confirm dialog before firing.

**Preset data model** (`presets.json`, single file = ordered trigger row;
created on the Pi at first capture, not deployed from the repo):
```
{ id, name,
  exclusive: bool,
  scope:  { main, motion, look, effect, master, singer_mode, singer_level, blackout, overlay, cycler },  // subsystems this preset may touch
  items:  { main:[id,...], motion:[id,...], look:[id,...], effect:[id,...] },  // ORDERED per category (bottom-first apply)
  levels: { master, singer_mode, singer_level,                  // applied only if scoped in
            blackout: null|'color'|'full', overlay: bool, cycler: null|<beats/look> }
}
```
Order only matters *within* a category — the output pipeline fixes precedence
*between* categories. Most stacked items target different fixtures and just
union, so order is only a per-channel tiebreaker (latest-in-list wins, matching
the live stacks).

**Apply (recall) algorithm** — `presets.apply_preset()` orchestration over
engine methods, reads live state via `get_state()` ordered lists:
- For each **scoped** playback category: `desired = items[cat]` (ordered).
  - *Exclusive*: stop live ids not in `desired`; play `desired` ids not already
    live, in list order (survivors keep their slot — additive-with-pruning; a
    full re-fade reorder of already-running items isn't enforced, by design).
  - *Additive*: play `desired` ids not already live, in order; stop nothing.
- **Scalars** (master / singer_mode / singer_level): *set-if-scoped*. Exclusive
  is a no-op on a value. Missing scene ids are skipped and reported, not fatal.
- **Subsystems** (blackout / overlay / cycler): *set-if-scoped*. Blackout via
  idempotent `engine.set_blackout(None|'color'|'full')` (a real setter, not the
  toggle). Overlay start/stop using the show's configured overlay scene; cycler
  start (at the stored beats/look division) / stop using the show's cycler
  scenes. The recall route resolves those scenes and passes them in. Scoped-on
  with nothing configured → skipped + reported in `notes`, never fatal.
- **Capture from live**: snapshot `get_state()` → `items` from the ordered
  lists, `levels` from current scalars + subsystem state (exclusive + fully
  scoped by default).

**v1 scope:** the four stacks + master + singer mode/level + blackout / overlay /
cycler — i.e. everything. (Overlay/cycler/blackout were added after the initial
MVP for full-snapshot fidelity.) Older presets saved before the subsystem add
lack those scope keys → normalize defaults them false, so they never touch
blackout/overlay/cycler (backward-compatible).

**Shipped as:**
- `presets.py` — storage + `capture_preset` + `apply_preset(preset, engine,
  scene_loader, overlay_scene=None, cycler_scenes=None)` (pure, Flask-free).
- `engine.py` — `set_blackout(mode)` idempotent setter (None/'color'/'full') for
  recall, alongside the existing toggle `blackout()`.
- `app.py` routes: `GET /api/presets`, `POST /api/presets` (body `{capture:true}`
  snapshots live; otherwise a full preset dict), `PUT`/`DELETE /api/presets/<id>`,
  `POST /api/presets/reorder`, `POST /api/preset/<id>/recall` (resolves the show's
  overlay + cycler scenes and passes them to `apply_preset`). (Capture is a flag
  on create, not a separate `/capture` route.)
- Home page (`lightboard_index.html`): ⭐ Presets trigger row — tap to recall,
  ＋ Capture (names + snapshots live, incl. subsystems), ✕ to delete.
- Settings (`settings.html`): full builder — name, additive/exclusive, per-
  subsystem scope checkboxes, per-category ordered item pickers (add/▲▼/✕),
  level controls, subsystem controls (blackout select / overlay toggle / cycler
  toggle + beats-per-look), ⟲ Fill-from-live, save/delete; reorderable list.
- Tests: `test_presets.py` (29 assertions: capture, additive vs exclusive, scope
  gating, scalars + subsystems set-if-scoped, blackout/overlay/cycler on-off and
  capture, missing-scene tolerance, normalize).

## Environment / workflow notes
- Pi: `pi@192.168.1.34`, dir `/home/pi/lightboard`, restart `sudo systemctl restart lightboard`.
- Repo: https://github.com/JKlaus1/Lightboard.git (public). Pi pulls; phone (Termux) pushes.
- Stage Messenger is a **separate app** (`/home/pi/stage-messenger/`) — not in this repo.
- Build against a fresh `git clone`; validate Python with `py_compile`, extracted
  JS with `node --check` (neutralize Jinja first), and a full Jinja render.
