# Pi Infrastructure & Services
State as of 2026-06-21. Companion to BOOT_FIX.md. Feeds the eventual installer / PI_FILE_MAP.
Records the things that are NOT in any app source file (network, tunnel, access, services).

## Host / services
- Pi 5, hostname `Lights` / `lights.local` (mDNS), Debian 13 (Trixie), NetworkManager.
- App services (Flask, run as user `pi`): `lightboard` (:5000), `stage-messenger` (:3000).
- `cloudflared` (systemd service, runs as root) — Cloudflare tunnel.
- `mediamtx` (systemd service, user `pi`, since 2026-10-04) — WebRTC relay for the
  mixer's low-latency listen. See "Low-latency listen (MediaMTX)" below.
- `go-librespot` (systemd service, user `pi`, since 2026-10-05) — Spotify Connect speaker
  "Stage Rig" playing into WING USB 1/2 → AUX 1. See "Spotify → WING (go-librespot)" below.

## Networking (NetworkManager profiles on the Pi)
- `netplan-wlan0-Lindentree` — home WiFi (netplan-managed).
- `android-hotspot` — phone hotspot for gigs (nmcli keyfile, autoconnect). Pi's gig internet uplink.
- `mixer-network` — eth0, pure DHCP (static .10 removed), `never-default yes`, `ignore-auto-dns yes`,
  autoconnect-priority 10. Local Art-Net / mixer / iPads; accepts a router DHCP reservation.
- `netplan-eth0` — dormant DHCP eth0 profile (out-prioritized by mixer-network).
- Routing model: **wlan0 owns the default route** (internet); eth0 never takes default, so a venue/rig
  network can't hijack the uplink. Multiple WiFi profiles autoconnect to whichever is in range.

## Internet uplink at gigs
- Pi gets internet via the **Android hotspot** (wlan0). eth0 stays local for Art-Net/mixer/iPads.
- The `/wifi` page (lightboard) can move the Pi onto venue WiFi when available, with auto-revert to the
  previous network if it fails or hits a captive portal.
- Captive-portal completion: the "Complete portal on Pi screen" button pops the venue login onto the
  Pi's touchscreen via the in-session watcher (kiosk_portal_watch.sh, triggered through the command
  file /tmp/lightboard_kiosk_cmd), waits up to ~3 min for you to tap through, then restores the touch
  kiosk. Hotspot remains the fallback for portal venues, and on the 3.5" screen the portal is cramped
  (built mainly for the future larger display).

## Cloudflare tunnel (cloudflared)
- Domain `stage-messenger.com` (Cloudflare Registrar). Tunnel name `stage-messenger`.
- Config `/etc/cloudflared/config.yml` (+ creds `/etc/cloudflared/<UUID>.json`). Lives under /etc
  because the service runs as root.
- Ingress:
  - `stage-messenger.com`        -> http://localhost:3000   (Stage Messenger — singer pages, PUBLIC)
  - `admin.stage-messenger.com`  -> http://localhost:5000   (Lightboard — PRIVATE)
  - catch-all -> 404
- Tunnel passes full path + query through, so auto-login links work over the domain.

## Cloudflare Access (Zero Trust, free tier)
Three self-hosted apps, each Allow -> your email (one-time PIN):
- `admin.stage-messenger.com`        — all of lightboard, locked to you.
- `stage-messenger.com/control` (path) — locked to you (redundant; lightboard has message control).
- `stage-messenger.com/mixer` (path)   — WING remote mixer + listen stream (added 2026-10-04).
  Must cover subpaths (`/mixer/api/*`, `/mixer/stream.mp3`). Check: a fresh private window on
  `/mixer/api/state` must get the Access login, not JSON or the Pi's 403.
Singer sender/receiver pages on `stage-messenger.com` stay public.

## Singer join links (control.html "Create Join Link" — dual-path)
- Local:    http://lights.local:3000/?name=NAME&role=sender|receiver   (on the band WiFi)
- Internet: https://stage-messenger.com/?name=NAME&role=sender|receiver (anywhere, via tunnel)

## This session's file changes
- touch.html        (updated) — footer IP self-refreshes every 15s.
- control.html      (updated) — dual-path join-link generator.
- app.py            (updated) — wired in wifi_routes (import + register call).
- wifi_routes.py    (new)     — venue WiFi page backend.
- templates/wifi.html (new)   — venue WiFi page (incl. captive-portal button).
- kiosk_portal_watch.sh (new, ~/) — in-session kiosk browser switcher for portal login.
- ~/.config/autostart/kiosk-portal-watch.desktop (new) — autostarts the watcher.
- /etc/polkit-1/rules.d/50-lightboard-nm.rules (new) — lets `pi` manage NetworkManager.
- /etc/systemd/system/lightdm.service.d/no-dri-wait.conf (new) — boot fix (see BOOT_FIX.md).
- /etc/systemd/system.conf.d/device-timeout.conf (new) — boot fix backstop.

## Hotspot boot-reconnect fix (2026-06-28)
The Pi would give up on the Android hotspot at boot: the hotspot radio idles
with no clients, and NetworkManager's default 4 autoconnect retries expire
before it reappears (unplugging eth0 forced a rescan, which is why that
"fixed" it). Standing fix on the Pi:
    nmcli connection modify android-hotspot connection.autoconnect-retries 0
    nmcli connection modify android-hotspot connection.autoconnect-priority 20
    nmcli connection modify android-hotspot 802-11-wireless.powersave 2
Plus: disable auto-timeout on the phone's hotspot so it keeps broadcasting.
Verify applied:  nmcli -f connection.autoconnect-retries,connection.autoconnect-priority connection show android-hotspot
(An optional systemd watchdog timer was discussed as a gig-reliability
backstop — documented only, not built.)

## Repo layout note (2026-07-03)
This doc, BOOT_FIX.md, and the OS-level support files now live in the
Lightboard repo (GitHub = single source of truth; project knowledge retired).
Support files are under infra/ in the repo; their DEPLOYED locations on the
Pi are unchanged and are what BOOT_FIX.md / this doc describe:
  infra/no-dri-wait.conf           -> /etc/systemd/system/lightdm.service.d/
  infra/device-timeout.conf        -> /etc/systemd/system.conf.d/
  infra/50-lightboard-nm.rules     -> /etc/polkit-1/rules.d/
  infra/kiosk_portal_watch.sh      -> /home/pi/
  infra/kiosk-portal-watch.desktop -> /home/pi/.config/autostart/

## Venue-install Pi (as-built 2026-07-05, first field-style build)
Separate Pi from the rack `Lights`. Bench unit: Pi 4B, hostname `Venue`, Trixie,
built by the wizard `install.sh` (ROLE=venue). Was reachable at `192.168.1.84`
on home WiFi during the bench.
- eth0: static `192.168.0.50/24`, profile `venue-artnet`, never-default. Art-Net
  **broadcast** `192.168.0.255`; CR011R (PKnight) lives on this segment.
- wlan1 = Panda MT7610U (MAC `9c:ef:d5:f6:19:35`): AP `Lights-Rig` / PSK
  `HarwoodLights01`, profile `venue-ap`, `10.42.0.1/24` (ipv4 shared). MUST be
  MAC-pinned to the Panda — the wizard mis-pinned it to onboard wlan0 on the
  first run (see PLAN.md 2026-07-05). Correct with:
    nmcli con modify venue-ap 802-11-wireless.mac-address 9C:EF:D5:F6:19:35
- wlan0 = onboard radio: optional house-WiFi client (bench: joined `Lindentree`).
- Boot: `KIOSK=yes` needs `systemctl set-default graphical.target` (now in
  install.sh `d6de8a6`); lightdm → openbox → chromium at :5000/touch.
- Kiosk admin gate (2026-07-06): 5s hold in any screen corner → optional PIN
  → nav to /, /touch-config, /library, /editor, /settings, /wifi. 3s hold on
  a scene button → that scene's editor (corner: release 3-5s = editor, 5s =
  admin). PIN = `kiosk_pin` (string) in config.json, unset = open; restart
  lightboard after changing. Sliding 60s unlock grace (server-side).
  Admin pages show a kiosk-only "← SHOW" button + 5-min idle return, and an
  on-screen keyboard — all gated on `location.hostname === 'localhost'`
  (`static/kiosk_nav.js` + `static/kiosk_osk.js`), so browsers on the rig
  WiFi are unaffected. The PIN gates the physical screen only; LAN access to
  /settings is open as before. The former native-prompt() gap is closed
  (2026-07-06): those dialogs are in-page formDialogs served by the OSK, and
  kiosk_osk.js now also loads on the show page (touch.html) so the admin-menu
  "Capture Look" name dialog gets a keyboard too. Leaving Touch Config with
  unsaved grid edits prompts (Save/Discard/Stay); the idle return auto-saves.
  Touch Config faders can also target the Master (Colour) or Singer dimmer
  directly (system faders), not just fixtures/groups.
- SSH: pubkey-only. authorized_keys = `josep@MSI`, `termux@phone`.

## Rack <-> Venue remote control (two-Pi link, validated 2026-07-05)
- Rack Pi (`Lights`) joins the venue AP as a WiFi client: profile `venue-link`
  (wlan0, ssid `Lights-Rig`, `ipv4.never-default yes`, autoconnect-priority 5).
  Gets a `10.42.0.x` lease; eth0 stays on the rack/mixer network.
- Master/slave: the rack Lightboard unicasts to `10.42.0.1:6454`; the venue's
  always-on artnet_receiver engages remote mode and re-outputs to its own CR011R
  (10 s of silence → auto-revert to local). Rack `config.json` `artnet_target`
  includes `10.42.0.1:0` (universe-0-only).
- No routing/NAT: the rack only ever talks to the venue Pi's AP-side IP, never
  the venue's 192.168.0.x Art-Net LAN.

- **Multiple universes over the link (2026-07-07):** the receiver handles each
  ArtDmx packet independently, so the rack can push several universes at once -
  just target them: `artnet_target` += `10.42.0.1:2, 10.42.0.1:3` sends
  universes 2 and 3 across while 0/1 stay local. The venue passes each straight
  through (no map: `uni = universe`) to its Art-Net driver, which broadcasts
  them; set each venue box's universe to match the rack's number and it works.
  **Match the box universe to the rack's; use `remote_universe_map` only as a
  fallback** (a box physically stuck on a universe it cannot change).
  `remote_universe_map` is a per-universe `{"in": out}` remap, e.g. `{"2":0}`
  emits rack universe 2 as venue universe 0; leave it unset for pass-through.
  Proven by test_remote.py section 9; demo live:
  `python3 test_remote.py --blast <venue-ip> 2,3 20` then
  `curl localhost:5000/api/remote-state` shows `universes:[2,3]`.
- `/api/remote-state` (2026-07-07): read-only JSON - `active`, `source` (master
  IP), `universes` (output universes after any remap), `age`, `timeout_s`.

## Per-Pi role model (config.json roles + pi_role, 2026-07-07)
One committed `config.json` can drive both Pis. Top-level keys are shared
defaults (currently the rack baseline); an optional `"roles": {"rack": {..},
"venue": {..}}` block holds per-Pi overrides overlaid at load
(`effective = top-level (+) roles[active]`). No `roles` block => behaves exactly
as before (backward-compatible).
- **Active role** = a gitignored one-word `pi_role` file at the repo root
  (`/home/pi/lightboard/pi_role`); absent => **rack**. It is the ONLY per-Pi
  local state, so `git pull` never collides with the choice.
- **Set it** in Settings -> **This Pi** -> Rack/Venue -> Restart (merge happens
  at boot). Or `POST /api/pi-role {"role":"venue"}` then restart; read with
  `GET /api/pi-role`.
- **Runtime saves** (touch grid, faders, DMX config, active show) fold their
  per-Pi keys into `roles[active]` (`save_config` / `CONFIG_ROLE_KEYS`),
  preserving the shared defaults and the other role's block.
- **Workflow nuance:** per-Pi runtime edits live in the role block *in the
  committed file*, so a `git pull` that changes the tracked `config.json`
  overwrites uncommitted local role-block edits. Commit `config.json` to
  distribute per-Pi settings; the shared top-level defaults stay stable.
- Files: app.py (`read_pi_role`/`_merge_role`/`save_config`, `/api/pi-role`,
  `/api/remote-state`), templates/settings.html (This Pi card), `.gitignore`
  (`pi_role`). Venue `remote_universe_map` stays UNSET (mirrors on universe 0 to
  its CR011R); an errant `{"0":1}` was set then reverted this session.

## UI restart needs passwordless sudo (infra/lightboard-restart.sudoers, 2026-07-07)
The web UI Restart button and the role-change "Restart now" call `/api/restart`,
which runs `sudo systemctl restart lightboard.service` with no TTY. Without a
sudoers grant that fails silently - the process never restarts and the UI
reconnects to the stale one (this was why a Rack->Venue change appeared not to
take). Fix (installed on the venue 2026-07-07):
    sudo install -m 0440 -o root -g root \
      /home/pi/lightboard/infra/lightboard-restart.sudoers \
      /etc/sudoers.d/lightboard-restart
    sudo visudo -c
Grants ONLY `systemctl restart` for `lightboard.service` and
`stage-messenger.service`. Deployed filename must have NO dot (files with `.`
in /etc/sudoers.d are ignored) -> `/etc/sudoers.d/lightboard-restart`. Confirm
the rule's path matches `command -v systemctl` (Debian Trixie: /usr/bin).

## Rack Pi disaster recovery (install.conf)
Unlike the Venue Pi, the rack (`Lights`) was built before `install.sh` existed,
so it never had a saved `install.conf`. A `rack-install.conf` is stashed
locally (off-repo, gitignored like the Venue Pi's) so a dead SD card can be
rebuilt with `./install.sh --config rack-install.conf --yes` instead of
re-deriving the config from memory. Two gaps the wizard model doesn't cover:

- **WiFi client profiles**: the live rack Pi runs three —
  `netplan-wlan0-Lindentree` (home), `android-hotspot` (gig internet uplink),
  `venue-link` (venue AP client, added by hand 2026-07-05 for two-Pi remote
  control). `install.sh`'s `WIFI_CLIENT` wizard step only creates one profile.
  After a `--config` restore, the other two need to be re-added by hand via
  `nmcli` — the stashed conf intentionally leaves `WIFI_SSID`/`WIFI_PSK` blank
  rather than embedding credentials in a generated file.
- **Cloudflare tunnel, no creds backup kept**: restore via interactive login,
  reusing the *existing* `stage-messenger` tunnel rather than creating a new
  one (a new tunnel would orphan the current DNS records + Access app
  bindings):
    1. `cloudflared tunnel login` — opens an authorize URL; approve from any
       browser signed into the Cloudflare account; `cert.pem` lands in
       `~/.cloudflared/` on the Pi.
    2. `cloudflared tunnel list` — confirms `stage-messenger` is visible, and
       gives its UUID.
    3. `sudo cloudflared tunnel token --cred-file /etc/cloudflared/<UUID>.json stage-messenger`
       — regenerates the credentials JSON for the *existing* tunnel (same
       UUID/DNS/Access bindings). Do **not** run `cloudflared tunnel create`
       here — that mints a new tunnel and orphans the current one.
    4. Re-run `./install.sh --config rack-install.conf --yes` — it finds the
       creds file and finishes writing `config.yml` + enabling the service.
  Note: `install.sh`'s guided tunnel steps (printed when no creds are found)
  now fork on this — step 2 checks `tunnel list`, with a 3a restore-existing
  path (`tunnel token`) and a 3b new-tunnel path (`tunnel create`).
- **Stage Messenger mixer extras (not covered by install.sh)** — after the base restore and
  `git clone` of StageMessenger into `~/stage-messenger`, in this order:
    1. `mixer_config.json` — recreate (`{"remote_enabled": true}` + anything below); it is
       gitignored. See "WING remote mixer".
    2. MediaMTX binary + unit + TURN key — see "Low-latency listen (MediaMTX)".
    3. Spotify: `ssh -t pi@lights.local "bash ~/stage-messenger/mixer/setup_pi_playback.sh"`
       (asound.conf, WirePlumber rule, go-librespot download — needs internet), then
       `ssh -t pi@lights.local "bash ~/stage-messenger/mixer/setup_spotify.sh"` (unit, sudoers,
       sign-in: approve the spotify.com/pair code). Search key: /mixer → Spotify → Library →
       Search → "Set up search" (or `mixer/set_spotify_search.sh`). See "Spotify → WING".


## Lights-Rig AP clients & deploy-over-AP (2026-07-11)

- Deploying from a phone/laptop joined to `Lights-Rig` works: the venue Pi is
  `pi@10.42.0.1` on the AP subnet (same host key as `192.168.1.84`; accept the
  new-address prompt once and known_hosts carries both).
- Static clients on `10.42.0.0/24` — keep static assignments in `.2`–`.9`
  (NM shared-mode DHCP leases start at ~`.10`):
  - `10.42.0.1` — venue Pi (AP host, gateway, chrony server when installed)
  - `10.42.0.5` — PKnight easynode "Artnet 1" (WiFi Art-Net→DMX receiver),
    gw 10.42.0.1, /24. TEST unit: works at the house; not yet gig-validated
    (2.4GHz bar RF + kiosk polling). Wired CR011R stays the show path.
    Quirk: the node's broadcast-address field only recomputes from the
    static IP once it has actually associated to the AP; before that it
    shows its own hotspot default (192.168.4.255).
- The sudoers drop-in (`infra/lightboard-restart.sudoers`) was found missing
  on the venue Pi 2026-07-11 (ssh deploy hit a sudo password prompt) and was
  reinstalled: `sudo install -m 0440 ~/lightboard/infra/lightboard-restart.sudoers
  /etc/sudoers.d/lightboard-restart && sudo visudo -c`. If a non-interactive
  `ssh ... "sudo systemctl restart lightboard"` ever prompts again, check this
  first.


## WING remote mixer (Stage Messenger `/mixer`, 2026-10-04)
Code is in the StageMessenger repo (`mixer/`); this records the OS/hardware side.
- **Hardware**: WING Rack USB-B -> Pi 5 USB (rack Pi `Lights`). Class-compliant, no driver:
  ALSA card `WING` (`arecord -l` card 0), 48 ch in/out, S24_3LE, 48 kHz only.
  `/proc/asound/WING/stream0` shows the format. Uses `arecord` (alsa-utils) + `ffmpeg`
  (libmp3lame) — both already installed on the rack Pi; no new packages, no new units
  (runs inside `stage-messenger.service`).
- **Network**: WING at `192.168.0.91` (name `Josephs-Wing`, fw 3.1) on the eth0
  `mixer-network` segment. eth0 is DHCP — give the WING (or Pi) a reservation, or update
  `mixer_ip` if it moves. Discovery: UDP `WING?` -> 2222 bound to eth0.
  Ports: OSC UDP 2223 (Pi ephemeral source), meters TCP 2222 + UDP 14135 inbound on the Pi.
- **USB output patch is owned by the Pi** (rewritten on start/reconnect, re-checked on
  listen and ~10 s after a recall changes it; "Re-patch USB" forces it). **v3.9 layout
  (2026-10-06; not yet run on the WING):** USB 1-32 Bus 1-16 (`BUS` 1..32), 33-40 Mtx 1-4
  (`MTX` 1..8), 42 ambient (the Ch chosen on the Listen card's Mic picker, default Ch 10;
  fallback `B` 4), 43-44 Main LR (`MAIN` 1/2), 45-46 Main 2 / subs (`MAIN` 3/4 — verify by
  ear), 47-48 Monitor 1 (`MON` 1/2). USB 41 untouched. Before v3.9: Main 1-2, Bus 3-34,
  Mtx 35-42, ambient 43. Don't use WING USB outs 1-40/42-48 for anything else.
- **Runtime files** (in `/home/pi/stage-messenger/`, gitignored, NOT in any repo):
  - `mixer_config.json` — currently `{"remote_enabled": true}`. Keys/defaults:
    `mixer_ip` 192.168.0.91, `remote_enabled` false, `usb_patch` true,
    `ambient.follow_channel` 10 / `grp` B / `in` 4, `bitrate` "128k",
    v3.9 `capture` {wing: hw:WING 48 ch, x32: hw:XLIVE 32 ch}.
    Rebuild after an SD restore or the mixer is LAN-only (tunnel requests get 403).
  - `mixer_state.json` — pending mute-group overrides (strip -> removed `#Mn` tags) and,
    since v1.9, `order` (shared channel display order). v3.9 adds `listen.sub_on` / `listen.sub_db`
    (sub blend, alongside `listen.opus_bitrate`) and `ambient` {wing: ch, x32: ch} (Mic picker). Safe to delete only when no
    override is active (order resets to console order).
- **Remote access** requires BOTH the Access app above and `remote_enabled: true`; the Pi
  also refuses tunnel requests that arrive without Cloudflare's Access JWT header.
  Kill switch: set `remote_enabled` false + `sudo systemctl restart stage-messenger`.
- **Data use over the hotspot** (Pi upload): ~58 MB/h per active listener at 128k
  (~29 MB/h at 64k) + ~46 MB/h per open mixer page (meters, 10 Hz). If the listening
  phone is also the hotspot, its plan counts the traffic twice (Pi up + phone down).

## Venue WiFi by hostname (Stage Messenger `netwifi`, 2026-10-07)
Why: a Stage-Messenger-only Pi (built with `mixer/setup_x32_pi.sh`, no Lightboard) had no `/wifi`
page, so moving it onto a new network meant finding its IP and running nmcli by hand.
- **Use:** from any device on the same network as the Pi, open **`http://<hostname>.local/`** and
  you land on the Venue WiFi page (scan / connect / saved / forget, auto-revert if no internet).
  It also lives at `http://<hostname>.local:3000/wifi`, and remotely at
  `https://<tunnel domain>/mixer/wifi` (WIFI button in the `/mixer` header).
- **Code (StageMessenger repo):** `netwifi/__init__.py` (port of Lightboard `wifi_routes.py`,
  loaded from `server.py` in a try/except like `/mixer`), `netwifi/wifi.html` (same page; API base
  follows the URL it was loaded from).
- **Gating:** `/wifi` = LAN only; tunnel requests (`Cf-Connecting-Ip`) get 403 on the API and a
  302 to `/mixer/wifi` for the page, so the public stage-messenger.com side never reaches it.
  `/mixer/wifi` = same gate as `/mixer`: tunnel needs `remote_enabled` in mixer_config.json AND a
  Cloudflare Access JWT (covered by the existing `/mixer` Access path rule).
- **OS pieces (installed by `sudo bash ~/stage-messenger/netwifi/install_netwifi.sh`, idempotent;
  `setup_x32_pi.sh` runs it on new builds):**
  - `/etc/polkit-1/rules.d/50-lightboard-nm.rules` -- same file as Lightboard's; lets `pi` drive NM
    from the service (no login session). Without it the page scans but connect/forget fail.
  - `avahi-daemon` enabled (mDNS `<hostname>.local`).
  - `stage-wifi-redirect.service` -- port 80 -> :3000 (`/` -> `/wifi`, other paths kept), 302 no-store.
    Script copied to `/usr/local/lib/stage-messenger/redirect80.py` (because /home/pi is 0700);
    DynamicUser, only CAP_NET_BIND_SERVICE. Re-run the installer after a pull that changes it.
- Portal button only appears when the Pi runs the kiosk watcher (`kiosk_portal_watch`), i.e. it has
  a touchscreen; headless Pis just report "no internet -- reverted" for captive-portal networks.
- On a Pi with both apps, Lightboard's `/wifi` on :5000 still works; the two keep separate connect
  state, so don't start connects from both at once.
- Hostnames must be unique per Pi for `.local` to resolve to the right box. Android browsers resolve
  `.local` inconsistently -- use an iPad/laptop, or the IP, if a phone can't find it.

## X32 listen-back (X-LIVE USB, StageMessenger v3.9, 2026-10-06)
- **Hardware**: X32 Rack (fw 4.15, `192.168.0.237` on eth0) with an X-LIVE card; card USB-B ->
  Pi 5 USB. ALSA card `XLIVE` (USB 1397:050a), 32 ch in/out, S24_3LE, 48 kHz (card pref
  `USB 32/32` — at 16 or 8 ch, card 25-32 doesn't exist). Only one program may open
  `hw:XLIVE` capture (same as `hw:WING`). Plugging the cable into the unpowered M32C instead
  shows nothing at all in `lsusb` — check that first.
- **Console routing owned by the Pi** (written on load, re-checked every ~15 s, "Re-patch"
  forces it): Card out 25-32 = User Out 1-8 (block value 26 = `UOUT1-8`); User Out 3 =
  ambient (input behind the chosen channel: local n, AES50 A 32+n, B 80+n, card 128+n,
  aux in 160+n), 4 = Out 14 (182, M/C sub), 5/6 = Monitor L/R (207/208), 7/8 = Out 15/16
  (183/184, Main L/R, tapped PRE in the current scene). **Card out blocks 1-24 are never
  touched** (AN1-8, AN9-16, AUX/TB — the SD multitrack). User Out 1/2 left OFF.
  X-LIVE USB channels: 27 ambient, 28 sub, 29/30 monitor, 31/32 Main LR.
- **Codes verified 2026-10-06** with the oscillator (no speakers connected) by sweeping user
  out codes 161-208 into card 25-32: 169-184 Out 1-16, 201-206 Aux out 1-6, 207/208 Mon L/R.
  `/node config/userrout/out` answers with numbers only (no names) — audio is the only proof.
- **numpy in the service venv** (sub blend in `picker.py`): `venv/bin/pip install 'numpy>=1.24'`
  (2.5.3 installed 2026-10-06; it is in `requirements.txt`). The venv has no system
  site-packages, so the system `python3-numpy` doesn't count. Listen works without it (no blend).
- Read-only probe: `python3 tools/x32_card_probe.py <ip>` (StageMessenger repo) dumps card blocks,
  user outs and card prefs.

## Low-latency listen (MediaMTX, StageMessenger v2.0, 2026-10-04)
WebRTC listen-back for `/mixer` (Opus, ~0.3–0.8 s) with the MP3 stream as automatic fallback.
- **Binary** (not in any repo, not apt): MediaMTX **v1.21.1** linux_arm64 in `/home/pi/mediamtx/`
  (pinned — the version the code was tested against). Install / reinstall:
    mkdir -p ~/mediamtx && curl -fsSL https://github.com/bluenviron/mediamtx/releases/download/v1.21.1/mediamtx_v1.21.1_linux_arm64.tar.gz | tar -xz -C ~/mediamtx && ~/mediamtx/mediamtx --version
  (The tarball's own `mediamtx.yml` in that folder is unused.)
- **Unit**: `/etc/systemd/system/mediamtx.service`, copied from the StageMessenger repo
  `mixer/mediamtx.service`; enabled. Runs `~/mediamtx/mediamtx ~/stage-messenger/mixer/mediamtx.yml`.
    sudo cp ~/stage-messenger/mixer/mediamtx.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now mediamtx
- **Config**: `mixer/mediamtx.yml` is tracked in the StageMessenger repo; MediaMTX hot-reloads
  it on change (a `git pull` is enough). Healthy log: RTSP `127.0.0.1:8554`, WebRTC
  `127.0.0.1:8889` + `:8189 (UDP/ICE)`, API `127.0.0.1:9997`.
- **Ports**: only **UDP 8189** listens on all interfaces (audio packets). RTSP (publisher =
  the mixer's ffmpeg), WHEP (setup, proxied by Flask under `/mixer/api/rtc/*` so Cloudflare
  Access guards it) and the API (Flask checks readiness/listener count) are localhost-only.
  STUN `stun.cloudflare.com:3478` lets the Pi learn its public address behind the hotspot.
- **Cloudflare TURN** (remote listeners): key created in the Cloudflare dashboard (Realtime →
  TURN, https://dash.cloudflare.com/?to=/:account/calls). Pricing: first 1,000 GB free, then
  $0.05/GB; Opus 96 k ≈ 43 MB/h per listener. Browsers may reach TURN over UDP 3478, TCP
  3478 or TLS 443; Cloudflare relays UDP to the Pi.
- **`mixer_config.json`** (gitignored) additions — defaults in `mixer/__init__.py`:
  - `rtc`: `enabled` true, `turn_key_id`, `turn_api_token` (secret — lives only in this
    file), `turn_ttl` 86400, `opus_bitrate` "96k", plus localhost `rtsp`/`api`/`whep` URLs.
  - MP3 fallback tuning: `cushion_s` 0.5, `max_queue_s` 1.0, `listen_target_s` 0.8
    (Joseph may have set 1.5 on 2026-10-04 to stop hitching — check the file).
  - Re-enter the TURN key after an SD restore (prompts; token not echoed or kept in history):
      cd ~/stage-messenger && read -p 'TURN Key ID: ' KID && read -s -p 'TURN API token: ' TOK && echo && KID="$KID" TOK="$TOK" python3 -c "import json,os;p='mixer_config.json';c=json.load(open(p));c.setdefault('rtc',{}).update(turn_key_id=os.environ['KID'].strip(),turn_api_token=os.environ['TOK'].strip());json.dump(c,open(p,'w'),indent=2);print('saved TURN key', c['rtc']['turn_key_id'][:6]+'…')" && sudo systemctl restart stage-messenger
    (`read -s` shows nothing while pasting — paste, then Enter.)
- **Without MediaMTX** everything still works on MP3; the page says why ("MediaMTX is not
  running on the Pi"). Kill switch: `"rtc": {"enabled": false}` + restart stage-messenger.
- **Data use** (Pi upload over the hotspot): WebRTC ≈ 43 MB/h per listener; MP3 fallback
  ≈ 58 MB/h at 128 k; mixer page meters ≈ 46 MB/h per open page.
- Diagnostics: `journalctl -u mediamtx -n 30 --no-pager` (sessions), and
  `curl -s http://127.0.0.1:9997/v3/paths/get/listen` (ready + readers).

## WING-LIVE recorder & Main/Alt (StageMessenger v2.1–v2.2, 2026-10-04)
No OS changes. The WING Rack has a WING-LIVE card (`/cards/$type` = WLIVE; two 128 GB SD
cards, `sdlink` IND). Channels' ALT sources point at the card (`altgrp` CRD). The Pi drives
REC/STOP/markers and `/io/altsw` over the existing OSC link; card/alt state is pushed by
the console. v2.3–v2.3.2 (2026-10-05) added card playback, scrub and marker editing — all over
the same OSC link, **no OS changes** (WING facts in PLAN.md / `mixer/wing.py`). The Pi also
**plays into** the WING over the same USB link since v2.4 — see "Spotify → WING (go-librespot)".

## Video feed (StageMessenger v3.4–v3.8.2, 2026-10-05/06)
**No OS changes** (ffmpeg/libx264/libopus were already installed; no new units or packages).
- **MediaMTX**: `mixer/mediamtx.yml` gained path `cam` (`source: publisher`) — hot-reloaded on
  `git pull`. Diagnostics: `curl -s http://127.0.0.1:9997/v3/paths/get/cam` (ready, tracks).
- **Cameras**: USB webcams are found by `/dev/v4l/by-id/*-video-index0` (rack Pi: NexiGo N930E
  on USB bus 3, mic = ALSA card `Webcam`). Wi-Fi cameras: the Pi pulls their stream over wlan0
  (no inbound port). Encoder = niced ffmpeg, only while someone watches (idle stop 15 s).
- **`mixer_config.json` → `cam`** (defaults in `mixer/__init__.py`): `enabled` true, `device` ''
  (pin one USB camera), `video_url` (config network camera, may carry user:password — lives only
  here; currently `http://192.168.1.195:8080/video`, Joseph's phone running IP Webcam),
  `video_name` (its label, default "Network camera"), `rtsp_transport` tcp, `idle_s` 15, `nice`
  15, `threads` 2, `gop_s` 2, `probe` true, starting values `quality` good / `feed` main1 /
  `delay_ms` 0 / `audio_bitrate` 128k. `rtc.opus_bitrate` default is now 128k.
  Kill switch: `"cam": {"enabled": false}` (or `rtc.enabled` false) + restart stage-messenger.
- **`mixer_state.json`** additions (page-made, win over the config): `cam` = feed, delay_ms,
  quality, audio_bitrate, source (chosen camera id), rot (per camera), **cams (Wi-Fi cameras
  WITH their URLs)**; `listen.opus_bitrate`. Back it up with the config before an SD rebuild.
- **Data use** (Pi upload): video at Good ≈ 1.1 GB/h per viewer (2.5 Mbps + 128k sound), High
  ≈ 1.6 GB/h, Low ≈ 0.3 GB/h, Min ≈ 0.17 GB/h — mind the hotspot.

## Spotify → WING (go-librespot, StageMessenger v2.4–v2.5.1, 2026-10-05)
Spotify Connect speaker **"Stage Rig"** on the rack Pi → WING USB in 1/2 → **AUX 1** → Main.
Controlled from the Spotify app (any network, same account) and from /mixer (Spotify card:
transport, seek, Library = playlists / start from a song / queue / up next / search).
- **Audio path**: go-librespot (ALSA) → `wing_pi` (plug → route 2→48 ch) → `wing_dmix` (dmix,
  48 ch S24_3LE 48 kHz, period 1024 / buffer 4096, `ipc_perm 0666`) → `hw:WING` playback. Capture
  (listen-back `arecord -D hw:WING`) is a separate stream and runs alongside (verified). Console
  side was already set: USB 1/2 = stereo pair ("USB 1/2"), AUX 1 source USB 1, AUX 1 → Main
  0 dB. The **AUX 1 fader is the only volume** (go-librespot runs fixed full scale).
- **PipeWire**: the kiosk desktop session (lightdm → openbox → Chromium) runs PipeWire +
  WirePlumber as `pi`, which claimed the WING as a sink/source. A WirePlumber 0.5 rule disables
  any `alsa_card.usb-BEHRINGER_WING*` for that session, so nothing in the desktop can lock or
  play into the console. Verify: `wpctl status | grep WING` → no output.
- **Files** (tracked in the StageMessenger repo `mixer/` → deployed location on the Pi):
    mixer/asound.conf                      -> /etc/asound.conf                (setup_pi_playback.sh)
    mixer/51-wing-ignore.conf              -> ~/.config/wireplumber/wireplumber.conf.d/
    mixer/go-librespot.yml                 -> ~/.config/go-librespot/config.yml (SYMLINK — git pull updates it;
                                              takes effect on `sudo systemctl restart go-librespot.service`)
    mixer/go-librespot.service             -> /etc/systemd/system/            (enabled)
    mixer/stage-messenger-spotify.sudoers  -> /etc/sudoers.d/stage-messenger-spotify (0440; NO dot in the name)
  Scripts (idempotent, run with `ssh -t` — they ask for the pi sudo password once):
  `mixer/setup_pi_playback.sh` (step A: asound.conf, WirePlumber rule, tone test, go-librespot
  download), `mixer/setup_spotify.sh` (step B: config link, unit, sudoers, sign-in, playback
  check), `mixer/set_spotify_search.sh` (search key; `--remove` to clear).
- **Binary** (not in any repo, not apt): go-librespot **v0.10.3** linux_arm64 (released
  2026-10-03) in `~/go-librespot/releases/v0.10.3/`, symlink `~/go-librespot/go-librespot`,
  `~/go-librespot/VERSION`, and that release's `config_schema.json` beside it. Dynamically
  linked; all libraries present on Trixie. Upgrade / pin:
    GOLIBRESPOT_VERSION=v0.x.y bash ~/stage-messenger/mixer/setup_pi_playback.sh
  then validate `mixer/go-librespot.yml` against the NEW `config_schema.json` (unknown keys are
  fatal: `additionalProperties: false`) and `sudo systemctl restart go-librespot.service`.
- **Config highlights** (`mixer/go-librespot.yml`): `device_name: Stage Rig`, `device_type:
  speaker`, `audio_device: wing_pi`, `bitrate: 320`, `external_volume: true` (app slider does
  nothing), `disable_autoplay: true` (stops at the end of a playlist/album), `credentials.type:
  device_auth` + `zeroconf_enabled: false` (only this account sees it; nobody on venue Wi-Fi can
  take it over), `prefer_firewall_friendly_ports: true`, `metadata.enabled: true` (song lists),
  `server` on **127.0.0.1:3678 only** — Stage Messenger proxies it under /mixer (Cloudflare Access).
- **Credentials — NOT in git, re-enter after an SD restore**:
  - Spotify login: `~/.config/go-librespot/state.json` (signed in as `magickbob`, 2026-10-05).
    **Re-pair**: /mixer → Spotify → ⋯ → Sign out & re-pair (shows the new code on the card), or
    `sudo systemctl stop go-librespot.service; rm ~/.config/go-librespot/state.json;
    sudo systemctl start go-librespot.service` and read the code in
    `journalctl -u go-librespot -n 20` (or run `mixer/setup_spotify.sh`).
  - Search key: `mixer_config.json` → `spotify.search_client_id`, `search_client_secret`,
    `search_saved_at`. Your own Spotify developer app (client-credentials, app-only — searches
    the public catalogue, never touches the account). The public Web API refuses
    go-librespot's session token (429), hence the separate app.
- **Search key renewal** (Spotify dashboard: secret lasts **180 days**; `search_secret_days`):
  current key saved 2026-10-05 → **renew by ~2027-04-03**. /mixer warns 21 days ahead (amber
  `SEARCH KEY N D` chip on the Spotify card) and turns red if Spotify refuses the key. Renew:
  developer.spotify.com/dashboard → the app → **ROTATE** the client secret (old one dies
  immediately) → /mixer Library → Search → paste → **TEST & SAVE** (saved only if a real search
  works; no restart). Fallback: `ssh -t pi@lights.local "bash ~/stage-messenger/mixer/set_spotify_search.sh"`.
- **Kill switch**: card → hold DISCONNECT (go-librespot `/player/stop`, no sudo) or ⋯ → Restart
  (needs the sudoers drop-in; without it the page shows the refusal). From SSH:
  `sudo systemctl restart go-librespot.service` (passwordless via the drop-in).
- **`mixer_config.json` → `spotify`** (defaults in `mixer/__init__.py`): `enabled` true, `api`
  http://127.0.0.1:3678, `aux` 1, `config_dir`, `market` US, the search key fields above.
  Kill switch for the whole card: `"spotify": {"enabled": false}` + restart stage-messenger.
- **Data use**: Spotify at 320 kbps ≈ 144 MB/h over the Pi's uplink (counts on the hotspot);
  `bitrate: 160` in go-librespot.yml halves it.
- **Diagnostics**: `systemctl status go-librespot`, `journalctl -u go-librespot -n 30 --no-pager`,
  `curl -s 127.0.0.1:3678/status` (204 = not signed in), `curl -s 127.0.0.1:3678/auth/code`,
  `cat /proc/asound/WING/pcm0p/sub0/status` (RUNNING while playing),
  `journalctl -u stage-messenger | grep spotify` (every command / kill / key change is logged).
