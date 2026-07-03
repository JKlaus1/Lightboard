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
