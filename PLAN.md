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

## Venue-install controller (next major project)

Standalone Pi (4 or 5) permanently installed at a venue, driving house fixtures.
Day-to-day: venue staff run basic looks from a 7" touchscreen (`touch.html`).
When Joseph is on site he takes full control — either directly from his tablet
via the Pi's own AP, or by driving the venue rig from his mixer-rack Pi as
extra Art-Net universes. Local-only: **no Cloudflare tunnel** on venue installs
(don't want to be on the hook for a business's connectivity).

Build order: **1) custom faders → 2) Art-Net remote mode → 3) installer bundle.**
Phase 1 is useful on the current rig immediately and needs no new hardware.

### Phase 1 — Custom fader system (touch UI) — AGREED SPEC
- Faders are grid citizens in the existing `touch_grid` config that can **span
  multiple rows/columns** (a vertical side fader = 1-wide, full-height cell).
  Orientation (vertical/horizontal) + span are per-fader config. With the 7"
  screen mounted landscape, Joseph expects a vertical fader on the side.
- Fader definition: `id`, `label`, `targets` (single fixture, list, or group),
  `channels` (`"dimmer"` default; any explicit channel offsets selectable),
  `mode` (`"limit"` | `"override"`), persisted level. Stored in `config.json`;
  live level set via a lightweight API endpoint (~20 posts/sec throttle).
- **Limit mode** = inhibitive submaster: multiplies engine output for its target
  channels. Full = invisible; pulled down = proportional cap on whatever the
  show is doing.
- **Override mode** = park: fader value wins outright for those channels,
  ignoring all scene/effect output. Has an **arm/disarm toggle** — disarmed, the
  fader does nothing (can't accidentally lock a channel at 0); armed at 0 it
  deliberately turns the target off. Armed override must be **visually
  unmistakable** on the venue screen (distinct color, e.g. red vs amber for
  limit) so anyone looking at the panel can tell the show is being overridden.
- Engine integration: new final stage in `_output_loop` composite, applied
  AFTER blackout (step 7), just before the output dict is written — limit
  multiplies, then armed overrides stamp values. No changes to scenes/effects/
  `genEvalFixture`. Same ~25ms responsiveness as the master fader.
- Files touched: `engine.py` (fader state + output stage), `app.py` (CRUD +
  level endpoints), `touch_config.html` (fader builder: name, orientation,
  span, target picker, channel selector, mode toggle), `touch.html` (render,
  axis-aware drag, arm/disarm, mode colors).

### Phase 2 — Art-Net remote mode (master/slave Pis)
- Venue Pi listens on UDP 6454 **always** (no toggle). Incoming Art-Net from
  the master engages remote mode automatically: local engine output suspended,
  frames piped straight to local output nodes, touchscreen shows "Remote
  control active" and local faders are bypassed (master has total say —
  limit/override stage skipped in remote mode).
- Watchdog: stream silent for N seconds → auto-revert to local control /
  default preset. Venue never left with dead lights.
- Master side: venue Pi is just another node in the planned `DMXRouter`
  fan-out; its fixtures patched as extra universes, unicast to 10.42.0.1:6454.

### Network architecture (venue install)
- **USB WiFi dongle = always-on AP.** Alfa AWUS036ACM (MT7612U — in-kernel
  driver, proven AP mode, 2.4+5GHz) or AWUS036ACHM for max range. NOT the
  AWUS036AXM (6GHz AP blocked by US regs; mt7921u BT-coex crashes on 6.6+
  kernels). NM profile: `802-11-wireless.mode ap`, band a / ch 149,
  `ipv4.method shared`, `ipv4.addresses 10.42.0.1/24` — venue Pi is always
  10.42.0.1 on its AP. **Pin the profile to the dongle by MAC**, not ifname
  (wlan0/wlan1 can swap on USB enumeration). 5GHz primary; drop to 2.4 if
  crowd attenuation bites (~30ft, ~20 bodies, near-LOS; mount Pi high).
- **Mixer-rack Pi joins the venue AP as a WiFi client** (its eth0 stays on the
  rack Art-Net LAN with the EdgeRouter X / Wing / PKnight dongles). Plain NM
  autoconnect — show up, power rack, it joins, Art-Net flows. No mode-switching
  scripts on the venue side. (Back-pocket alternative if the link disappoints:
  NM dispatcher swap so the venue dongle becomes a client of the rack SSID,
  with 60s+ debounce against mid-show flapping — documented, not built.)
- **Built-in wlan0 (venue Pi)** = optional client for venue house WiFi
  (updates/NTP only; house WiFi client isolation confirmed to block
  tablet→Pi control, hence the AP approach).
- **Subnets must not overlap:** AP 10.42.0.x vs rack LAN vs eth0 Art-Net
  192.168.0.x (.185/.186 dongles).
- Wired Art-Net nodes: configure via their web UI at the manual's default
  static IP (temporarily move a laptop/eth0 onto that subnet), assign a
  static in the rig scheme. Bench-configure before install day. Rescue paths:
  `nmap -sn` sweep; ArtIpProg (DMX-Workshop) for MAC-based IP reprogramming.

### Phase 3 — Installer bundle
- `install.sh` in-repo: packages, systemd units, avahi, AP profile,
  touchscreen (DSI) setup, kiosk autostart. One-time setup only — installed
  Pis update via `git pull && restart`, so the installer only changes when
  infrastructure changes (edit script, push; no packaging/rebuild step).
- Screen candidates: Pi official 7" DSI (800×480) or Waveshare 7" IPS
  (1024×600); `touch.html` grid/font sizes need upward scaling either way.
