# Pi Infrastructure & Services
State as of 2026-06-21. Companion to BOOT_FIX.md. Feeds the eventual installer / PI_FILE_MAP.
Records the things that are NOT in any app source file (network, tunnel, access, services).

## Host / services
- Pi 5, hostname `Lights` / `lights.local` (mDNS), Debian 13 (Trixie), NetworkManager.
- App services (Flask, run as user `pi`): `lightboard` (:5000), `stage-messenger` (:3000).
- `cloudflared` (systemd service, runs as root) — Cloudflare tunnel.

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
Two self-hosted apps, each Allow -> your email (one-time PIN):
- `admin.stage-messenger.com`        — all of lightboard, locked to you.
- `stage-messenger.com/control` (path) — locked to you (redundant; lightboard has message control).
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

