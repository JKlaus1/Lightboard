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

### Phase 3 — session handoff 2026-07-05 (first hardware bench + two-Pi validation)
First real hardware run of the wizard installer on a fresh Pi OS Lite (Trixie,
Pi 4B, hostname `Venue`). All four bench gates passed and the two-Pi remote-
control chain — the whole point of the venue project — is proven end to end.

**Shipped this session:**
- `install.sh` kiosk fix (commit `d6de8a6`): `mod_kiosk()` never ran
  `systemctl set-default graphical.target`, so a `KIOSK=yes` build installed and
  enabled the full lightdm/openbox/chromium stack yet still booted to a bare
  `multi-user.target` console. One line added right after the `KIOSK=yes` guard.
  Every prior kiosk install would have hit this.

**Bench results (Venue Pi, Pi 4B, Trixie aarch64):**
- Gate 1 (pre-probe): Panda MT7610U = **wlan1**, MAC `9c:ef:d5:f6:19:35`, USB
  `0e8d:7610`; onboard radio = wlan0 (`dc:a6:32:f7:38:bd`). Trixie confirmed.
- Gate 2 (wizard): ran clean. As-built install.conf deviated from the headless
  bench plan (HDMI monitor was attached): `SCREEN=hdmi`, `KIOSK=yes`,
  `WIFI_CLIENT=yes` (joined home WiFi `Lindentree`). ROLE=venue, eth0 static
  `192.168.0.50`, ARTNET broadcast.
- Gate 3 (verify): lightboard enabled-but-not-started post-install (expected —
  see reboot note); started clean, Art-Net out `192.168.0.255`, :5000 up.
- Gate 4 (DMX): CR011R → real fixture, correct output. End to end.

**KNOWN INSTALLER ISSUE — AP pinned to the wrong radio (NOT yet fixed in code):**
`pick_wifi_radio` pre-selects (`ON`) the *first* wlan interface in the radiolist,
which is the onboard `wlan0`. The wizard was accepted on that default, so
`venue-ap` pinned to `dc:a6:32:f7:38:bd` (wlan0) instead of the Panda. wlan0 was
also the WiFi client, so the AP couldn't activate there and wlan1 sat NO-CARRIER.
Fixed by hand:
    nmcli con modify venue-ap 802-11-wireless.mac-address 9C:EF:D5:F6:19:35
    nmcli con up venue-ap
Installer TODO: default the radiolist `ON` to the *external* dongle (the non-
built-in radio), or hard-warn when the chosen AP MAC equals the onboard radio's.
This will bite every venue build until fixed.

**Reboot behaviour (by design, confirmed):** `mod_enable` only `enable`s units;
`mod_eth0` deliberately doesn't force-activate profiles mid-script (won't drop an
eth0 SSH session). Everything comes up on the final `sudo reboot`. Confirmed:
after reboot, lightboard + AP + eth0 static all came up unattended once the two
manual fixes (graphical.target, AP MAC) were applied — one now in code, one TODO.

**Two-Pi remote control — VALIDATED (closes the Phase 2/3 goal):**
Primary topology (not the dispatcher-swap alternative):
- Venue Pi always hosts `Lights-Rig` on the Panda (wlan1, `10.42.0.1`).
- Rack Pi (`Lights`) joins Lights-Rig as a plain NM client — new profile
  `venue-link` (wlan0, `ipv4.never-default yes`, autoconnect-priority 5). Leased
  `10.42.0.181`. eth0 hardwired to home LAN for the bench (mixer-rack net in field).
- Master/slave bridge (artnet_receiver remote mode): rack unicasts universe 0 at
  `10.42.0.1:6454`; venue receiver engages remote mode and re-outputs to its own
  CR011R. No routing/NAT — rack never touches the 192.168.0.x Art-Net LAN.
- Rack `config.json`: `artnet_target` += `10.42.0.1:0` (universe-0-only; leaves
  the rack's own rig targets untouched). Betopper LPC1818 (7ch @ addr 164)
  patched on the rack; a rack scene drove the venue fixture live. Confirmed:
  `REMOTE control engaged — Art-Net from 10.42.0.181 (universe 0)`.
- Deploy caveat: `10.42.0.1:0` carries the rack's real universe 0, so the venue
  mirrors whatever the rack has at those addresses. For an independent venue rig,
  give it its own universe + venue `remote_universe_map`.

**SSH / tooling:** Venue Pi is pubkey-only (password auth off). authorized_keys =
`josep@MSI` (laptop) + `termux@phone`. From the laptop, Pi is driven via
PowerShell Posh-SSH — Windows `ssh.exe` yields no capturable output under the
agent tool, but Posh-SSH's in-process .NET SSH does.

**Next session:**
1. Fix the AP-radio-pick issue in install.sh (default `ON` to external dongle /
   warn on onboard-MAC collision).
2. Rack install.conf disaster-recovery capture (still outstanding from 07-04).
3. touch.html fader scaling on the real panel; venue `remote_universe_map` if an
   independent venue universe is wanted.

### Phase 3 — session handoff 2026-07-06 (AP-radio fix + rack disaster recovery)
Remote session (laptop, no hardware). Closes both open items from 07-05.

**Shipped this session:**
- `install.sh` AP-radio-pick fix (commit `def8de1`): `pick_wifi_radio` now
  detects each `wl*` interface's bus via `/sys/class/net/$iface/device` (USB
  dongle vs. onboard SDIO/mmc) and pre-selects the first USB radio as the
  whiptail default, instead of always defaulting to the first interface
  `ip -o link show` happens to list (usually onboard `wlan0` — the bug that
  bit the venue build). If the onboard radio is picked anyway and more than
  one radio was on offer, a hard `whiptail --yesno` confirmation is required.
  Falls back to the first entry, no warning, when only one radio exists
  (bench Pi with no dongle attached). Verified via mocked bash logic tests
  (onboard-first, dongle-first, onboard-only orderings) — real whiptail
  dialog not exercised (no hardware this session).
- Rack Pi (`Lights`) disaster-recovery `install.conf` drafted and stashed
  locally (off-repo, gitignored — same handling as the Venue Pi's). Captures
  role=rack, eth0 dhcp (`mixer-network`), unicast Art-Net targets including
  the manually-added `10.42.0.1:0` venue-link line, Stage Messenger + tunnel
  enabled. `WIFI_SSID`/`WIFI_PSK` deliberately left blank in the stashed
  file — not writing credentials into a generated file unprompted.
- PI_INFRA.md += "Rack Pi disaster recovery" section: documents the WiFi
  multi-profile gap (installer only recreates one of the rack's three client
  profiles — `netplan-wlan0-Lindentree`, `android-hotspot`, `venue-link` —
  the other two need manual `nmcli` re-add post-restore) and the Cloudflare
  tunnel restore procedure (no creds tarball kept, so interactive
  `cloudflared tunnel login` + `tunnel token --cred-file` against the
  *existing* `stage-messenger` tunnel — not `tunnel create`, which would
  orphan current DNS + Access bindings).

**New installer gap found (not fixed, logged for later):** `install.sh`'s own
guided tunnel setup, when no creds are found, prints `cloudflared tunnel
create ${TUNNEL_NAME}` as step 2 — correct for a genuinely new tunnel, wrong
for restoring an existing one. TODO: have the guided message distinguish
new-tunnel vs. restore-existing-tunnel.

**Next session:**
1. touch.html fader scaling on the real panel; venue `remote_universe_map` if
   an independent venue universe is wanted.
2. Installer TODO: distinguish new-tunnel vs. restore-existing-tunnel in the
   guided Cloudflare tunnel setup message (see above).
3. Fill in `WIFI_SSID`/`WIFI_PSK` on the stashed rack-install.conf by hand
   (not committed — local file only) so it's actually restore-ready.

### Phase 3 — session handoff 2026-07-06 later session (kiosk touch-UI buildout)
Long laptop session (PowerShell git — dedup rules apply to Downloads folder
the same as Termux). Ten commits, every one verified byte-identical between
GitHub HEAD and the validated build before proceeding. Closes prior items 1
(fader scaling: 44px fix shipped; Joseph confirmed LONG_PRESS_MS=300 and
FADER_SEND_MS=50 feel right on the real panel — no tuning needed) and 2
(tunnel-restore message, commit `828a5f5`). Item 3 (rack-install.conf WiFi
creds) still on Joseph.

**Shipped this session (`4bcf48f`..`c8e9428`), in order:**
- `4bcf48f` touch.html: fader-track min touch target 30->44px (matches the
  scene-btn 44px convention).
- `a0433dd` **Generalized footprint model**: any grid cell (scene, action,
  new `label` type, fader) carries w/h 1-12 and spans its footprint in both
  the kiosk (`touch.html`) and the builder (`touch_config.html`).
  `footprintError` validates fit/overlap uniformly; `_clean_cell` in app.py
  clamps w/h server-side. New cell type `{type:"label", label, w, h}` —
  static, non-interactive divider/heading; `.grid-label` CSS in touch.html.
- `8294134` **Font sizing + drag preview**: `touch_grid.font_size` (global,
  8-72, default 13, drives `--grid-font` CSS var) + per-cell `font_size`
  (0 = inherit). Builder drag now previews the FULL footprint at the hovered
  anchor, green=valid / red=invalid via read-only `moveWouldFail` (same
  swap-and-validate as the real drop).
- `337befd` **Per-button colors**: `list_library_scenes` derives `color` per
  scene — `_pod_hex` is a Python port of editor.html's podCssColor emitter
  mix (KEEP THE TWO IN SYNC); effect scenes use `primary`, static scenes use
  dimmer-weighted pod average of step 1. Builder modal: AUTO chip / palette
  swatches (from /api/palette rgb recipes, #000000 filtered) / native custom
  picker. Cells store `color` (manual) + `auto_color` (snapshot at assign
  time, same staleness semantics as `name` — re-assign to refresh). Kiosk:
  `.colored` class + CSS vars for border/wash/active-glow; 8-digit-hex alpha
  tints (`cc+'1a'/'33'/'80'`); declared after type-* variants so manual color
  overrides them. `_clean_hex` validates '#rrggbb' server-side.
- `828a5f5` install.sh guided tunnel steps fork: step 2 = `tunnel list`;
  3a restore-existing (`tunnel token --cred-file`, skip DNS) / 3b new
  (`tunnel create` + `route dns`). PI_INFRA TODO cleared.
- `7ff4c99` **Kiosk admin gate**: 5s hold in any screen corner (90px zones,
  document-level listener — no dead zones; matured hold swallows the release
  click) -> PIN pad if config.json `kiosk_pin` set (string; unset/empty =
  open) -> nav overlay (Show Board / Touch Config / Library / Editor /
  Settings / WiFi). `GET/POST /api/touch/unlock` (timing-safe via
  secrets.compare_digest; PIN never leaves server). NEW `static/` dir (Flask
  default serving): `kiosk_nav.js` included on all 7 admin templates —
  activates ONLY when `location.hostname === 'localhost'` (i.e. only the
  kiosk chromium, which loads localhost:5000) — floating "<- SHOW" button +
  5-min idle auto-return to /touch. iPads via lights.local see nothing.
  Gate z-index 1100 sits above the remote banner (1000); corner hold still
  works during remote mode (banner events bubble to document).
- `98d8441` **3s scene-hold -> editor + grace window**: 3s hold on a scene
  button opens /editor/<id> (route auto-picks static vs effect editor).
  Corner dual-threshold: both holds arm on one press; at 3s a hint pill
  shows ("Release -> Edit Scene · Keep holding -> Admin"); release 3-5s =
  editor, 5s = admin wins. PIN (when set) gates the editor hold too, via
  `gateTarget` on the PIN flow. Server-side SLIDING 60s grace window
  (`_kiosk_unlock_until`): successful unlock opens it, every gated check
  inside renews it — back-to-back edits skip the PIN; re-locks 60s after
  last gated use. Server-side because the kiosk runs --incognito.
  Context-menu suppressed + user-select/touch-callout off on the touch
  surface (kiosk and iPad).
- `c8e9428` **On-screen keyboard** `static/kiosk_osk.js`, injected by
  kiosk_nav.js (kiosk-only, zero template churn): document-level focusin
  delegation (dynamic modal inputs work automatically); QWERTY + one-shot
  shift + symbols layer; numeric pad for type=number; keys are
  pointerdown+preventDefault so focus never drops; edits fire real `input`
  events; Chromium number/email inputs lack selection APIs -> append/trim-
  at-end fallback. Body padding + scrollIntoView keep the field visible.

**Testing discipline established this session:** exact expected diffstat is
computed with `git diff --stat` against the parent commit BEFORE delivery
(estimates once mismatched and tripped the dedup check — `98d8441`).
Every push verified: fresh clone of HEAD, `tr -d '\r'` byte-diff against the
validated build (PowerShell/CRLF normalization is expected and harmless).

**Venue Pi config knobs added:** `kiosk_pin` (string) in config.json —
set + `sudo systemctl restart lightboard` to arm the gate; grace window is
`_KIOSK_GRACE_S = 60` in app.py.

**Next session — PHASE B (kiosk hands-on usability):**
1. Joseph exercises editor/settings/library/wifi on the real 7" 1024x600
   panel via the gate and reports what's unusable — targeted fixes follow.
   Known candidates: editor density at 7", modal sizing, OSK overlap in
   dense modals.
2. **prompt() refactor (4 call sites)**: index.html:1082 (preset naming),
   settings.html:1730/1732/1733 (channel-slot label + range lo/hi). Native
   prompt() dialogs cannot be served by any JS keyboard — refactor to async
   in-page dialogs (which kiosk_osk.js then serves automatically).
3. Backlog: Phase C "capture current live look as scene" quick-save in the
   admin nav (engine-side work); venue `remote_universe_map` if an
   independent venue universe is wanted; rack-install.conf WiFi creds
   (Joseph, local file).

### Phase 3 — session handoff 2026-07-06 (evening — touch-UI validation + faders + exit guard)
Hardware bench session on the Venue Pi (`192.168.1.84`, 7" 1024x600 panel).
Validated the three previously-shipped-but-unvalidated touch-UI commits,
scrapped the audio experiment, fixed three UX papercuts found on the real
panel, and shipped two new features. Origin HEAD `da88fa3`; every push
byte-verified against the validated build.

**Validated on hardware (no code change):**
- `7fa2ead` touch-config edit mode — tapping a filled cell reopens it with the
  current assignment prefilled/highlighted; edits apply on tap-off.
- `504d2d4` OSK-compatible formDialog replacing native prompt() — preset naming
  + settings channel-slot label/range now type via the kiosk on-screen keyboard.
- `a20a9c5` Capture Look (admin menu) + preset-as-grid-button — capture a live
  look, place it as a grid button, recall it from the show. Full gauntlet green.

**Scrapped:**
- `fc09ad4` kiosk_sfx audio feedback — worked, but the 52Pi panel speakers are
  far too quiet to hear in a bar even at max volume. Reverted whole (`ec02a3e`).
  Settles the audio-path question: don't rely on panel audio. `aplay -l` showed
  the card enumerating and `speaker-test -D default` produced tone (hardware
  fine), so no ALSA default override was pursued — it's a loudness dead-end.

**Fixes (papercuts found on the real panel):**
- `4ff0381` Touch Config grid canvas — was trapped in the body's 480px centred
  column, so a large (e.g. 20x20) grid started ~1/4 in from the left and ran off
  the right. Now the canvas breaks out to full viewport width in a horizontal
  scroller (`#grid-scroll`), columns fixed at 72px, grid `width:max-content;
  margin:0 auto` — centres when it fits, scrolls with BOTH edges reachable when
  it doesn't. Controls/size-fields stay in the tidy 480 column.
- `9cb9885` Show-page OSK — `kiosk_osk.js` shipped on the 7 admin templates but
  NOT touch.html, so the admin-menu "Capture Look" name dialog (which lives on
  the show page) had no keyboard. Added the include; it self-gates on localhost,
  so iPads via lights.local are unaffected.
- `dcd9829` Admin-menu single-tap — the 5s corner-hold armed `gateSuppress` to
  swallow the release-click, but a long press often emits NO click, leaving the
  flag set to eat the user's first menu tap ("tap twice to open"). Now
  auto-clears 700ms after the hold matures.

**New features:**
- `952bea8` Master / Singer **system dimmer faders**. A fader def may carry a
  `system` field (`"master"`|`"singer"`); such a fader drives the engine scalar
  directly — same path as the Show Board sliders (`set_master` /
  `set_singer_level`) — instead of resolving DMX channels.
  `_resolve_fader_channels` yields empty keys (never touches DMX stage 8a);
  `set_fader_level` routes system faders to the scalar setter with the lock
  released first (no deadlock); `get_fader_state` reports their level straight
  from `_master_level`/`_singer_level`, so the fader mirrors the Show Board live
  and follows clear-all's reset to 100%. No ARM button (it IS the live control;
  mode is forced to override). Touch Config's fader editor gained a **System
  dimmer** picker that hides mode/channels/targets when set. `test_faders.py`
  +7 assertions (all 31 pass). Files: engine.py, app.py, templates/touch.html,
  templates/touch_config.html, test_faders.py.
- `da88fa3` Touch Config **unsaved-changes exit guard**. Leaving the builder via
  the kiosk "← SHOW" button with pending grid edits now prompts
  (Stay / Discard & Exit / Save & Exit); the 5-minute idle auto-return saves
  silently instead of losing work. Dirty is detected by snapshotting exactly
  what saveConfig persists (`{cols,rows,font_size,cells,faders}`) and comparing
  — no per-edit instrumentation to slip past. Generic hooks in kiosk_nav.js
  (`window.kioskExitGuard` veto + `window.kioskIdleSave` idle-save); only Touch
  Config sets them today — other admin pages exit as before. Files:
  static/kiosk_nav.js, templates/touch_config.html.

**Phase B status:** both Phase-B items from the prior handoff are done — the
usability pass surfaced the three papercuts above (all fixed) and the prompt()
refactor is validated live (504d2d4 + the show-page OSK include). No further
editor/settings density issues reported this session.

**Next session:**
1. Field/venue install of the Venue Pi rig — installer + two-Pi remote chain are
   validated; the physical venue install is the remaining milestone.
2. Optional polish: extend the unsaved-changes guard to the editor/settings
   pages if wanted; label auto-suggest ("MASTER"/"SINGER") when a system dimmer
   is picked in the fader editor.
3. Backlog carried forward: Phase C "capture live look as scene" quick-save in
   the admin nav; venue `remote_universe_map` for an independent venue universe;
   rack-install.conf WiFi creds (Joseph, local file); INSTALL.md Imager
   walkthrough; DMXRouter master-side fan-out (Opus-class).
