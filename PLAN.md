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

Build order: **1) custom faders ✅ → 2) Art-Net remote mode ✅ → 3) installer bundle (NEXT).**

### Phase 1 — Custom fader system (touch UI) — ✅ SHIPPED (2026-07-03)
Built to spec plus WYSIWYG/drag extensions. As-built reference:

- **Data model:** fader defs live in `config.json` under `custom_faders`:
  `{id, label, orientation, w, h, mode: "limit"|"override",
  channels: "intensity"|[1-indexed offsets], targets: {fixtures:[], groups:[]},
  level}`. Grid cells reference them as `{fader_id, name}` in `touch_grid.cells`.
- **Channel resolution** (`engine._resolve_fader_channels`): `"intensity"` →
  hardware dimmer channel if the fixture has one (engine parks it at 255, so a
  limit there is a clean intensity scaler); fixtures with **no dimmer** (e.g.
  Betopper LPC1818) → all pod color channels; movers → `channel_roles.dimmer`.
  Explicit offset lists resolve per-fixture with out-of-range offsets ignored.
  Groups expand to members at resolve time; re-resolved on `load_show`.
- **Engine stage 8a** in `_push_to_dmx`: after blackout blend (8), before the
  raw channel test (8b — kept on top so bench probing always works). Limits
  multiply in definition order; armed overrides then stamp `level*255`.
  **Armed override beats blackout by design.** Snapshot tuple rebuilt only on
  change, read under the existing lock — no per-tick cost.
- **Override arm/disarm:** disarmed = inert (can't accidentally park a channel
  at 0). Armed = red cell + glow (limit = amber). Arm state deliberately does
  NOT survive a restart; levels DO (2s debounced write-back to config.json —
  no SD hammering at drag rate).
- **API:** GET/POST `/api/touch/faders` (defs + live state + pickable
  fixtures/groups), POST `/api/touch/fader/<id>/level`, `/arm`. Faders also in
  `/api/state` and in GET `/api/touch/config` (single fetch for touch.html).
- **WYSIWYG grid** (both `touch.html` and the builder): slot index i renders
  at row ⌊i/cols⌋, col i%cols — gaps included; rows pinned `1fr`. Faders
  occupy their true w×h footprint; covered slots aren't rendered/tappable.
  `faderFootprint()` clamps spans to grid bounds; `placementError()` blocks
  assign/resize that doesn't fit or overlaps (alert names the blockers).
- **Builder drag-to-reposition:** long-press ~300ms (`LONG_PRESS_MS`) lifts a
  cell; drop moves, or swaps with an occupied slot; faders re-validate at the
  destination and snap back on error. Quick tap = assign modal; page scroll
  unaffected (movement before the hold cancels it).
- **Fader drag:** pointer-capture, axis-aware, ~50ms throttle
  (`FADER_SEND_MS`) with trailing flush so the final position always lands.
  `fetchState` syncs level/arm from the server unless actively dragging.
- **Mobile viewport:** `touch.html` uses `100dvh` + safe-area padding so
  phone-browser nav bars don't clip the grid (no effect on chromeless kiosk).
- **Tests:** `test_faders.py` — 24 checks (resolution incl. dimmerless +
  mover, limit/override semantics, override-beats-blackout, state survival
  across reconfig and `load_show`). All prior suites still green.
- Files touched: `engine.py`, `app.py`, `templates/touch.html`,
  `templates/touch_config.html`, `test_faders.py` (new).
- **Field-tuning knobs:** `LONG_PRESS_MS`, `FADER_SEND_MS`, fader track
  min-sizes — revisit on the real 7" screen (only phone/iPad tested so far).

### Phase 2 — Art-Net remote mode (master/slave Pis) — ✅ SHIPPED (2026-07-03)
Built to spec; live-verified over WiFi (Termux phone → Pi, banner + DMX
monitor + watchdog revert all confirmed). As-built reference:

- **`artnet_receiver.py` (new):** always-on UDP :6454 listener started at app
  boot (no toggle). Parses ArtDmx (opcode 0x5000, full 15-bit port-address)
  via module-level `parse_artdmx()`; `build_artdmx()` is the shared packet
  builder used by tests and the blast tool. Guards: only ArtDmx acted on
  (ArtPoll/PollReply from output nodes ignored); packets from the Pi's OWN
  IPs dropped (SIOCGIFADDR enumeration, refreshed 60s) so the local sender
  can never feedback-loop into remote mode; malformed/truncated dropped.
- **Engine:** `handle_remote_frame(uni, data, src_ip)` engages remote mode on
  first packet and straight-pipes the full-length frame to `_dmx.set_channels`
  (full 512 write so master-cleared channels propagate). Driver has its own
  lock → receiver-thread writes are safe; ≤1 local frame overlaps at
  engagement. `_push_to_dmx` early-returns while remote is active — the whole
  local composite INCLUDING fader stage 8a and blackout is bypassed (master
  has total say). Scene/blend ticks keep running, so revert resumes the local
  show exactly where it should be. Watchdog in `_output_loop`: stream silent
  > `REMOTE_TIMEOUT_S` (default **10s**; config `remote_timeout_s`, floor 1s)
  → revert to local on the same tick. Optional `remote_universe_map`
  ({incoming: local}) in config.json; identity pass-through by default.
  `_remote_universes` is a frozenset replaced atomically (get_state-safe).
- **State/UI:** `get_state()` carries `remote: {active, source, universes,
  age, timeout_s}`. `touch.html` shows a full-screen pulsing blue
  "REMOTE CONTROL ACTIVE — local controls bypassed — master: <ip>" overlay
  that also swallows all touches; markup sits BEFORE the inline `<script>`
  (script-placement rule). Picked up by the existing 2s `/api/state` poll.
- **Tests:** `test_remote.py`, 24 checks (packet round-trip, junk rejection,
  engagement, remap, local suspension incl. armed-override bypass, watchdog
  revert, get_state, end-to-end loopback with real socket). Doubles as the
  manual tester: `python3 test_remote.py --blast <pi-ip> [uni] [secs]` fires
  a ~30fps red chase — must run from a machine that is NOT the Pi (own-IP
  guard). Termux works; use the numeric IP (no mDNS in Termux).
- **Master side (still to come with `DMXRouter`):** venue Pi is just another
  fan-out node; its fixtures patched as extra universes, unicast to
  10.42.0.1:6454.

### Network architecture (venue install)
- **USB WiFi dongle = always-on AP.** ✅ ORDERED (2026-07-03): **Panda
  Wireless PAU0B** (MT7610U, USB ID 0e8d:7610 — same chipset/driver as the
  Alfa ACHM; in-kernel mt76, proven stable AP mode, detachable 5dBi RP-SMA).
  AC600 (433Mbps @5GHz) is ample for control + a few unicast Art-Net
  universes. Range-upgrade path: swap to AWUS036ACHM later = update the MAC
  pin only (same driver). **On arrival: verify `lsusb` shows 0e8d:7610 and
  mt76 binds.** Known quirk: low TX power (11dBm) until regulatory domain is
  set — bake `country=US` into the Phase 3 AP profile. mt7610u has **no DFS
  in AP mode** — fine, plan already pins non-DFS ch 149.
  Adapters evaluated and REJECTED for AP duty (all client-grade on Linux):
  AWUS036AXM/mt7921u (BT-coex kernel crashes on 6.6+, client-triggered AP
  oopses reported on Pi 5 into 2026); TP-Link T3U Plus + Netgear A6150
  (RTL8812BU — in-kernel rtw88 decent as of 6.12 but repeated Pi breakage
  across kernel updates); Netgear A7500 (RTL8852AU), WNA3100 (Broadcom, no
  AP); BrosTrend AC1L/AC5L/AX7PL (Realtek house; their "Linux support" =
  client mode). Rule: **listing says chipset mt7612u/mt7610u or it's a no.**
  NM profile: `802-11-wireless.mode ap`, band a / ch 149,
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

### Phase 3 — Installer bundle — ✅ SHIPPED (2026-07-04, commit bed3495)
Scope grew (by design) from venue-only to ONE wizard-driven `install.sh` that
builds EITHER Pi flavour from a fresh Pi OS image. As-built reference:

- **Two roles:** `rack` (Lightboard + Stage Messenger + optional tunnel;
  eth0 = DHCP `mixer-network`, WiFi-client uplink) and `venue` (Lightboard
  only, local-only; eth0 static, broadcast Art-Net, own AP). Role sets
  defaults; every option is individually overridable.
- **Wizard = whiptail** (raspi-config's TUI; works over SSH, no X). Answers
  saved to `install.conf` (gitignored, per-machine) → every install is
  reproducible: `./install.sh --config install.conf --yes` rebuilds the same
  rig non-interactively. `--check` validates a conf + prints the plan without
  touching anything; `--wizard` forces re-asking. **A saved rack conf is the
  rack Pi's disaster-recovery story.**
- **Art-Net out (selectable):** `broadcast` (venue default) / `unicast` /
  `keep` (config.json untouched — rack default). Broadcast = directed subnet
  broadcast derived from the static eth0 /24 (e.g. .50 → .255); validator
  refuses broadcast without static eth0 so limited-broadcast-out-the-wrong-
  interface can't happen. `artnet.py` grew an unconditional `SO_BROADCAST`
  setsockopt (permissive only — zero effect on the rack's unicast targets).
  **Broadcast rationale:** every node on the segment hears every universe and
  pulls its own port-address → add output by configuring the NODE's universe,
  no per-node IP/MAC in Lightboard, no ARP — the entire CR011R failure class
  (IP-derived MAC, ARP INCOMPLETE, ping-revival) is structurally gone.
  → resolves the 2026-07-03 open question: **broadcast**, unicast retained
  for the WiFi dongles.
- **Network modules:** eth0 static `venue-artnet` OR DHCP `mixer-network`
  (both never-default, ignore-auto-dns) or skip; optional WiFi-client profile
  (autoconnect-retries 0 + powersave 2 + priority 20 — the hotspot fix baked
  in); optional AP on ANY radio (internal or dongle) — wizard radiolists the
  wlan MACs and pins the profile by MAC, 5GHz ch149 or 2.4GHz ch6, reg-domain
  US persisted via raspi-config + a `wifi-regdom` oneshot before NM.
- **Screens:** `dsi_auto` (official 7" AND official-protocol clones — Hosyond
  5" IPS confirmed driver-free, so 5" is the minimum-viable venue screen at
  zero extra code) / `dsi_waveshare` (needs its overlay — verify size tag per
  model) / `hdmi` (any size; touch = standard USB HID; rotated-panel xinput
  hook documented in the kiosk autostart) / `headless`. Kiosk (lightdm
  autologin → openbox → chromium `--kiosk` :5000/touch) is a separate toggle.
  **SPI screens dropped by design** — BOOT_FIX.md's no-dri-wait.conf is a
  rack-SPI legacy fix and is deliberately NEVER installed by this script
  (KMS displays must not have it).
- **Messenger module:** clones/pulls StageMessenger to
  `/home/pi/stage-messenger`, own venv, `stage-messenger` unit (PORT=3000
  env → server.py).
- **Tunnel module (optional, independent of role):** GUIDED, not automatic —
  creds are secrets and can't live in the public repo. Installs cloudflared
  (Cloudflare apt repo), writes ingress config.yml (root→3000 only if
  messenger installed, admin.<domain>→5000, 404 catch-all), then restores
  creds from a backup tarball OR prints the five one-time `cloudflared`
  commands and leaves the service disabled until creds exist. Re-run with
  `--config --yes` to finish after login.
- **config.json handling:** when Art-Net mode ≠ keep, patches
  dmx_driver/artnet_target then `git update-index --skip-worktree config.json`
  so `git pull` never clobbers the per-machine config (repo's tracked
  config.json is effectively the rack's). Undo to pull an upstream config
  change: `--no-skip-worktree`.
- **Fresh-bare-Pi path** (header of install.sh): Raspberry Pi Imager sets
  hostname/user pi/SSH/WiFi in its own GUI → boot → apt install git → clone →
  `./install.sh`. Imager covers the OS layer; the wizard covers the rest.
- **Validated offline:** bash -n; venue-broadcast + rack-full confs through
  `--check`; four illegal combos rejected (broadcast w/o static eth0, AP w/o
  MAC, PSK <8, headless+kiosk); tunnel config.yml rendered both messenger
  variants; kiosk autostart rendered + `sh -n`; SO_BROADCAST patch idempotent
  + py_compile. NOT yet run on real hardware.

### Phase 3 — session handoff 2026-07-03 (evening)
Bench-verification session. Both pieces of Phase 3 hardware proven; no installer
code written yet. Key decision: **rack Pi stays DHCP (portable); venue install
gets static eth0.** So the eth0-static work belongs in `install.sh`, NOT on the
rack Pi's `mixer-network` profile (which stays pure DHCP).

**AP dongle (Panda PAU0B) — ✅ PROVEN end-to-end.**
- Enumerates `0e8d:7610`, driver `mt76x0u` binds clean. It's **wlan1**, MAC
  `9c:ef:d5:f6:19:35` (built-in radio is wlan0). Pin AP profile to this MAC —
  both radios support AP, so ifname is unsafe.
- `iw reg set US` unlocks ch149 TX power (12→15 dBm; world-domain throttle
  confirmed as PLAN predicted). Reg domain is NOT persistent — installer must
  bake `country=US` (NM `802-11-wireless.band a` + a persistent reg setting).
- Working AP profile recipe (create, then deleted from rack Pi at session end):
  `nmcli con add type wifi ifname '*' con-name venue-ap 802-11-wireless.ssid
  <SSID> mode ap band a channel 149 mac-address 9C:EF:D5:F6:19:35
  wifi-sec.key-mgmt wpa-psk proto rsn pairwise ccmp group ccmp psk <PSK>
  ipv4.method shared ipv4.addresses 10.42.0.1/24`. Phone joined, got
  10.42.0.51 via NM shared DHCP, reached :5000. NAT/dnsmasq all good.

**CR011R wired Art-Net node — ✅ PROVEN it converts Art-Net→DMX.**
- Lit a real fixture when fed valid unicast Art-Net (u0, full-on). Hardware and
  DMX path are good. Every failure this session was Pi-side, not the node.
- **MAC is IP-derived: last 3 MAC bytes = last 3 IP octets.** At 192.168.0.187
  → MAC `02:4d:48:a8:00:bb` (`187`=bb, `168`=a8, `0`=00). Matches OLED. TRAP:
  reading the MAC off another LAN (UniFi showed `a8:01:bb` at a .1.x IP) gives
  the wrong value — the MAC follows the IP. Pinning the wrong MAC permanent is
  what caused hours of "works after a ping, then goes deaf."
- Node OLED submit gesture: click to select, **hold Enter ~3s to commit**.
  Blue in/out LED: **off = standby (not committed), solid = Art-Net→DMX out,
  blinking = DMX→Art-Net in.** Set Transmit mode = `Artnet → dmx`.
- Node port-address net0/sub0/uni0 matches rig universe 0. Mask 255.255.255.0,
  host octet can't be 0/255 (.187 fine).

**Root cause of the whole evening (documented so we don't repeat it):** manual
`ip addr add` on eth0 while NM owns it via the DHCP `mixer-network` profile.
On a direct cable (no DHCP server) NM periodically reasserts the profile, tears
down the manual IP + the 192.168.0.0/24 connected route, and Art-Net frames
silently drop (non-blocking sendto). A `ping` transiently revived it → the
misleading "ping makes DMX work for a bit" symptom. Even production dongles
(.185/.186) showed ARP INCOMPLETE during the route collapse. Fix = a REAL
static eth0, not a manual add.

**Next session (Phase 3 build):**
1. Bench: create `artnet-bench` NM profile (eth0, `autoconnect no`,
   `ipv4.method manual 192.168.0.10/24`, `never-default yes`,
   `ignore-auto-dns yes`, ipv6 disabled) — a dedicated static that does NOT
   touch `mixer-network`. Bring up to finish/repeat node verification cleanly
   (no ping babysitting). Tear down + `up mixer-network` when done.
2. Write `install.sh`: eth0 static (venue), AP profile (country=US, ch149,
   MAC-pinned, 10.42.0.1/24 shared), packages, systemd units, avahi, kiosk.
   Node registration: venue nodes are just extra `artnet_target` entries;
   remember IP-derived MAC when documenting per-node setup.
3. Open q for install.sh: broadcast vs unicast — **RESOLVED 2026-07-04:
   broadcast** (see Phase 3 as-built above). Unicast retained as a wizard
   option for the WiFi dongles.

### Phase 3 — session handoff 2026-07-04
Installer-build session (remote, no hardware — Joseph on a gig). Shipped
commit `bed3495`: wizard `install.sh` (as-built above), `artnet.py`
SO_BROADCAST, `.gitignore` += install.conf. Note: the 07-03 session's venue-
only installer + artnet patch were never pushed (gig week); this commit
supersedes them — nothing from that build exists in history.

**Decisions this session:**
- Broadcast Art-Net = venue default (question resolved; rationale in Phase 3).
- SPI screens killed permanently. Minimum viable screen = 5" DSI (Hosyond
  IPS confirmed official-protocol/driver-free → covered by `dsi_auto`).
  Bigger = HDMI, no ceiling (USB HID touch). "HDMI + keyboard for setup,
  then pull it headless" works: kiosk with no display is harmless, or re-run
  installer with SCREEN=headless.
- Tunnel fully optional + role-independent (guided module, creds never in repo).

**Next session (hardware bench):**
1. Run the wizard for real on a spare/venue Pi from a fresh Pi OS image —
   first end-to-end test (offline validation only so far). Watch: chromium
   package name, cloudflared apt repo on trixie, AP radiolist output.
2. Venue bring-up: broadcast → CR011R end-to-end (node needs only subnet +
   universe + Artnet→dmx; no registration).
3. Generate + stash a **rack install.conf** (disaster-recovery for the rack
   Pi) — walk the wizard on the rack or hand-write from PI_INFRA.md.
4. Deferred: INSTALL.md (Imager settings walkthrough) + optional first-boot
   wizard hook; touch.html scaling pass on the real 5"/7" panel
   (LONG_PRESS_MS / FADER_SEND_MS / track min-sizes); DMXRouter fan-out
   (Opus-class) for master-side multi-output.
