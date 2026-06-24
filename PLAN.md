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

## Roadmap (agreed order: GitHub → B → A)

### 1. GitHub migration (done with this commit)
Repo is the source of truth. Pi **pulls only** (public repo, no auth). Pushes
happen from **Termux** (phone) where GitHub auth already works.
Steady-state deploy: edit the `~/Lightboard` clone → commit → push →
on Pi `git pull && sudo systemctl restart lightboard`. **No direct Pi patching anymore.**

### 2. B — Concurrency engine upgrade (next, its own chat)
Convert the **single** motion / look / effect slots into **composited stacks**,
like main scenes (`_active_scenes` + `_composite_scene_dmx`) already are, so
multiple can run at once — e.g. different motions/looks on different movers, and
layered effects.
- Today: `_current_motion_id`, `_current_look_id`, `_current_effect_scene` are
  single; playing a new one replaces the old (true even manually, not just presets).
- Model the upgrade on the existing main-scene stack.
- Watch: compositing/merge rules, freeze queue (`_pending_*`), blackout/overlay
  interaction, and the shape of `get_state()` (UI + presets read it).

### 3. A — Presets (after B)
Master "recall" bundles referencing existing library items + scalar controls.

**Data model (sketch):**
```
{ id, name,
  exclusive: bool,
  scope: { main, motion, look, effect, master, singer_mode, singer_dimmer, blackout, overlay, cycler },  // which subsystems this preset may touch
  items:  [ {scene_id, type} ],          // type: main | motion | look | effect (multiples allowed after B)
  levels: { master, singer_mode, singer_level, blackout, overlay, cycler }  // applied only if scoped in
}
```

**Rules:**
- **Scope** = the checklist of subsystems the preset is allowed to manage.
  Unscoped subsystems are **never touched** (e.g. leave movers running untouched
  by scoping out motion+look; note an effect targeting movers is the one exception).
- **Exclusive** (within scoped subsystems only): also clear active items *not* in
  the preset. **Additive**: set only the preset's items, leave the rest.
- **Levels/scalars** are *set-if-scoped*; Exclusive is a no-op on a value.
- **Capture current state**: build a preset by snapshotting the live rig
  (`engine.get_state()`).

**Implementation:** mostly `app.py` orchestration over existing engine methods
(`play_scene`/`stop_scene`, `play_motion_scene`/`stop`, `play_look_scene`/`stop`,
`play_effect_scene`/`stop`, plus level setters); engine reads via `get_state()`.
Storage `presets.json` (or `presets/*.json`). Builder UI in settings; trigger row
on the home page (`lightboard_index.html`).

## Environment / workflow notes
- Pi: `pi@192.168.1.34`, dir `/home/pi/lightboard`, restart `sudo systemctl restart lightboard`.
- Repo: https://github.com/JKlaus1/Lightboard.git (public). Pi pulls; phone (Termux) pushes.
- Stage Messenger is a **separate app** (`/home/pi/stage-messenger/`) — not in this repo.
- Build against a fresh `git clone`; validate Python with `py_compile`, extracted
  JS with `node --check` (neutralize Jinja first), and a full Jinja render.
