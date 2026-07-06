#!/usr/bin/env bash
# =============================================================================
# Lightboard rig installer — rack or venue, wizard-driven
# =============================================================================
# ONE script builds either flavour of Pi from a fresh Raspberry Pi OS install:
#
#   RACK  — full mixer-rack rig: Lightboard (:5000) + Stage Messenger (:3000)
#           + optional Cloudflare tunnel. eth0 = DHCP "mixer-network"
#           (portable across venues), WiFi client for internet uplink.
#   VENUE — permanently-installed house controller: Lightboard only,
#           local-only (no tunnel, no messenger). eth0 = static Art-Net LAN,
#           broadcast Art-Net out by default, own AP for tablets.
#
# Everything is selectable in a guided whiptail wizard (same TUI as
# raspi-config; works over SSH, no X needed). Answers are saved to
# ./install.conf so any install is REPRODUCIBLE:
#
#     ./install.sh                     # wizard (or re-use existing install.conf)
#     ./install.sh --config FILE       # non-interactive from a saved conf
#     ./install.sh --check             # load+validate conf, print plan, exit
#     ./install.sh --wizard            # force the wizard even if a conf exists
#     add --yes to skip the final confirmation
#
# FRESH-PI PATH: flash Pi OS with Raspberry Pi Imager (set hostname, user
# `pi`, SSH, and — for rack — WiFi in Imager's own GUI), boot, then:
#     sudo apt-get update && sudo apt-get install -y git
#     git clone https://github.com/JKlaus1/Lightboard.git /home/pi/lightboard
#     cd /home/pi/lightboard && ./install.sh
# After that the Pi updates like any other: git pull && restart the unit(s).
#
# NOTES
# - install.conf is per-machine: keep it OUT of git (.gitignore).
# - The tunnel module is GUIDED, not fully automatic: tunnel credentials are
#   secrets and can't live in a public repo. It installs cloudflared, writes
#   the ingress config, and either restores creds from a backup tarball you
#   provide or walks you through `cloudflared tunnel login` once.
# - SPI screens are not supported (dropped by design — see BOOT_FIX.md for
#   the legacy rack SPI fix; none of that applies here). DSI auto-detect
#   covers the official 7" AND official-protocol clones (e.g. Hosyond 5" IPS).
# =============================================================================
set -euo pipefail

APP_DIR="/home/pi/lightboard"
MSG_DIR="/home/pi/stage-messenger"
MSG_REPO="https://github.com/JKlaus1/StageMessenger.git"
VENV="${APP_DIR}/.venv"
MSG_VENV="${MSG_DIR}/.venv"
BOOT_CFG="/boot/firmware/config.txt"
CONF_FILE="${APP_DIR}/install.conf"

log()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mxx %s\033[0m\n' "$*" >&2; exit 1; }

# ── CLI ─────────────────────────────────────────────────────────────────────
MODE_CHECK=no; FORCE_WIZARD=no; ASSUME_YES=no
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONF_FILE="$2"; shift 2;;
    --check)  MODE_CHECK=yes; shift;;
    --wizard) FORCE_WIZARD=yes; shift;;
    --yes)    ASSUME_YES=yes; shift;;
    *) die "Unknown option: $1";;
  esac
done

# ── Config defaults (overridden by wizard / conf file) ──────────────────────
ROLE="venue"                       # rack | venue
HOSTNAME_NEW=""                    # empty = keep current
ETH0_MODE="static"                 # static | dhcp | skip
ETH0_IP="192.168.0.50"             # static mode only (/24 assumed)
ARTNET_MODE="broadcast"            # broadcast | unicast | keep
ARTNET_TARGETS=""                  # unicast: "ip, ip:uni, ..."
WIFI_CLIENT="no"                   # join an existing WiFi for internet
WIFI_SSID=""; WIFI_PSK=""
AP_ENABLE="no"                     # host an access point
AP_MAC=""                          # radio to pin the AP profile to (REQUIRED if AP)
AP_SSID="Lights-Rig"; AP_PSK=""
AP_BAND="a"; AP_CHANNEL="149"      # a/149 = 5GHz non-DFS; bg/6 = 2.4GHz
AP_IP="10.42.0.1"
SCREEN="dsi_auto"                  # dsi_auto | dsi_waveshare | hdmi | headless
KIOSK="yes"                        # chromium kiosk at :5000/touch
MESSENGER="no"                     # Stage Messenger (:3000)
TUNNEL="no"                        # Cloudflare tunnel (guided)
TUNNEL_DOMAIN="stage-messenger.com"
TUNNEL_NAME="stage-messenger"
TUNNEL_BACKUP=""                   # optional path to a creds backup tarball
KIOSK_URL="http://localhost:5000/touch"

# ── Wizard ──────────────────────────────────────────────────────────────────
need_whiptail() {
  command -v whiptail >/dev/null 2>&1 && return 0
  log "Installing whiptail for the setup wizard"
  sudo apt-get update -y && sudo apt-get install -y whiptail
}

w_menu()  { whiptail --title "Lightboard installer" --menu  "$1" 20 74 8 "${@:2}" 3>&1 1>&2 2>&3; }
w_input() { whiptail --title "Lightboard installer" --inputbox "$1" 12 74 "$2" 3>&1 1>&2 2>&3; }
w_yesno() { whiptail --title "Lightboard installer" --yesno "$1" 12 74; }

pick_wifi_radio() {  # radiolist of wlan interfaces -> echoes chosen MAC
  local args=() line iface mac first=ON
  while read -r line; do
    iface=$(echo "$line" | awk -F': ' '{print $2}')
    mac=$(echo "$line" | grep -oE '([0-9a-f]{2}:){5}[0-9a-f]{2}' | head -1)
    [ -n "$mac" ] || continue
    args+=("$mac" "$iface" "$first"); first=OFF
  done < <(ip -o link show | grep -E ': wl')
  [ ${#args[@]} -gt 0 ] || { warn "No WiFi interfaces found."; return 1; }
  whiptail --title "Lightboard installer" --radiolist \
    "Which radio hosts the AP?\n(Pinned by MAC — interface names can swap on USB enumeration. The external dongle is the one that disappears when unplugged.)" \
    18 74 6 "${args[@]}" 3>&1 1>&2 2>&3
}

run_wizard() {
  need_whiptail
  ROLE=$(w_menu "What is this Pi?" \
    "rack"  "Mixer-rack rig: Lightboard + Stage Messenger (+ tunnel)" \
    "venue" "Venue install: Lightboard only, local-only") || die "Wizard cancelled."

  # Role defaults (every one still individually overridable below)
  if [ "$ROLE" = "rack" ]; then
    ETH0_MODE="dhcp"; ARTNET_MODE="keep"; MESSENGER="yes"; TUNNEL="yes"
    WIFI_CLIENT="yes"; AP_ENABLE="no"
  else
    ETH0_MODE="static"; ARTNET_MODE="broadcast"; MESSENGER="no"; TUNNEL="no"
    WIFI_CLIENT="no"; AP_ENABLE="yes"
  fi

  HOSTNAME_NEW=$(w_input "Hostname (mDNS name; keep unique per Pi). Blank = keep '$(hostname)'." "") || die "Cancelled."

  ETH0_MODE=$(w_menu "eth0 (wired) role?" \
    "static" "Static Art-Net LAN (venue: fixed IP, never-default)" \
    "dhcp"   "DHCP 'mixer-network' (rack: portable, never-default)" \
    "skip"   "Leave eth0 unconfigured") || die "Cancelled."
  if [ "$ETH0_MODE" = "static" ]; then
    ETH0_IP=$(w_input "eth0 static IP (/24 assumed). Avoid known nodes: .185/.186 rack dongles, .187 CR011R." "$ETH0_IP") || die "Cancelled."
  fi

  ARTNET_MODE=$(w_menu "Art-Net output mode?  Broadcast: every node on the wired segment hears every universe and pulls its own port-address — add outputs by configuring the NODE, no per-node IP/MAC in Lightboard, no ARP. Recommended for installs." \
    "broadcast" "Subnet broadcast (needs static eth0)" \
    "unicast"   "Unicast to listed node IPs (WiFi dongles need this)" \
    "keep"      "Don't touch config.json (keep repo/current targets)") || die "Cancelled."
  if [ "$ARTNET_MODE" = "unicast" ]; then
    ARTNET_TARGETS=$(w_input "Unicast targets, comma-separated. 'IP' = all universes, 'IP:N' = only universe N. e.g. 192.168.0.185, 192.168.0.186:1" "$ARTNET_TARGETS") || die "Cancelled."
  fi

  if w_yesno "Join an existing WiFi network as a client?\n(Internet uplink for updates / tunnel. More networks can be added later via the /wifi page or nmcli.)"; then
    WIFI_CLIENT="yes"
    WIFI_SSID=$(w_input "WiFi SSID" "$WIFI_SSID") || die "Cancelled."
    WIFI_PSK=$(w_input "WiFi password" "$WIFI_PSK") || die "Cancelled."
  else WIFI_CLIENT="no"; fi

  if w_yesno "Host an access point on this Pi?\n(Tablets/phones connect directly — venue standard. Internal radio OR a USB dongle; you pick the radio next.)"; then
    AP_ENABLE="yes"
    AP_MAC=$(pick_wifi_radio) || die "Cancelled (no radio picked)."
    AP_SSID=$(w_input "AP SSID" "$AP_SSID") || die "Cancelled."
    AP_PSK=$(w_input "AP password (8+ chars)" "$AP_PSK") || die "Cancelled."
    local bandpick
    bandpick=$(w_menu "AP band?" \
      "5"   "5 GHz ch149 (default — clean; radio must support 5GHz AP)" \
      "2.4" "2.4 GHz ch6 (better through crowds/walls)") || die "Cancelled."
    if [ "$bandpick" = "5" ]; then AP_BAND="a"; AP_CHANNEL="149"; else AP_BAND="bg"; AP_CHANNEL="6"; fi
  else AP_ENABLE="no"; fi

  SCREEN=$(w_menu "Display?" \
    "dsi_auto"      "DSI auto-detect: official 7\", Hosyond 5\", clones" \
    "dsi_waveshare" "Waveshare 7\" DSI panel (needs its overlay)" \
    "hdmi"          "HDMI monitor/touchscreen (any size)" \
    "headless"      "No screen (control from a browser only)") || die "Cancelled."
  if [ "$SCREEN" = "headless" ]; then KIOSK="no"
  elif w_yesno "Boot straight into the touch UI (Chromium kiosk at :5000/touch)?"; then KIOSK="yes"; else KIOSK="no"; fi

  if [ "$ROLE" = "rack" ]; then
    if w_yesno "Install Stage Messenger (:3000)?"; then MESSENGER="yes"; else MESSENGER="no"; fi
  fi

  if w_yesno "Install the Cloudflare tunnel?\n(Remote access over the internet. GUIDED: needs a Cloudflare account + domain; creds via one-time login or a backup tarball. Skip for local-only rigs.)"; then
    TUNNEL="yes"
    TUNNEL_DOMAIN=$(w_input "Tunnel domain" "$TUNNEL_DOMAIN") || die "Cancelled."
    TUNNEL_NAME=$(w_input "Tunnel name" "$TUNNEL_NAME") || die "Cancelled."
    TUNNEL_BACKUP=$(w_input "Path to creds backup tarball (blank = interactive login during install)" "") || die "Cancelled."
  else TUNNEL="no"; fi

  save_conf
  log "Saved answers to ${CONF_FILE} (re-run non-interactively with --config)"
}

save_conf() {
  cat > "$CONF_FILE" <<CONF
# Lightboard installer answers — per-machine, keep OUT of git.
ROLE="$ROLE"
HOSTNAME_NEW="$HOSTNAME_NEW"
ETH0_MODE="$ETH0_MODE"
ETH0_IP="$ETH0_IP"
ARTNET_MODE="$ARTNET_MODE"
ARTNET_TARGETS="$ARTNET_TARGETS"
WIFI_CLIENT="$WIFI_CLIENT"
WIFI_SSID="$WIFI_SSID"
WIFI_PSK="$WIFI_PSK"
AP_ENABLE="$AP_ENABLE"
AP_MAC="$AP_MAC"
AP_SSID="$AP_SSID"
AP_PSK="$AP_PSK"
AP_BAND="$AP_BAND"
AP_CHANNEL="$AP_CHANNEL"
AP_IP="$AP_IP"
SCREEN="$SCREEN"
KIOSK="$KIOSK"
MESSENGER="$MESSENGER"
TUNNEL="$TUNNEL"
TUNNEL_DOMAIN="$TUNNEL_DOMAIN"
TUNNEL_NAME="$TUNNEL_NAME"
TUNNEL_BACKUP="$TUNNEL_BACKUP"
CONF
}

# ── Load / validate ─────────────────────────────────────────────────────────
if [ "$FORCE_WIZARD" = "yes" ] || { [ ! -f "$CONF_FILE" ] && [ "$MODE_CHECK" = "no" ]; }; then
  run_wizard
fi
[ -f "$CONF_FILE" ] || die "No config at ${CONF_FILE}. Run the wizard first (./install.sh)."
# shellcheck disable=SC1090
. "$CONF_FILE"

derive_broadcast() { echo "${ETH0_IP%.*}.255"; }  # /24 directed broadcast

validate_conf() {
  case "$ROLE" in rack|venue) ;; *) die "ROLE must be rack|venue";; esac
  case "$ETH0_MODE" in static|dhcp|skip) ;; *) die "ETH0_MODE must be static|dhcp|skip";; esac
  case "$ARTNET_MODE" in broadcast|unicast|keep) ;; *) die "ARTNET_MODE must be broadcast|unicast|keep";; esac
  case "$SCREEN" in dsi_auto|dsi_waveshare|hdmi|headless) ;; *) die "SCREEN invalid";; esac
  if [ "$ARTNET_MODE" = "broadcast" ] && [ "$ETH0_MODE" != "static" ]; then
    die "ARTNET_MODE=broadcast requires ETH0_MODE=static (broadcast address is derived from the static /24)."
  fi
  if [ "$ETH0_MODE" = "static" ]; then
    echo "$ETH0_IP" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || die "ETH0_IP '$ETH0_IP' is not an IPv4 address."
    local host="${ETH0_IP##*.}"
    { [ "$host" -ge 1 ] && [ "$host" -le 254 ]; } || die "ETH0_IP host octet must be 1-254."
  fi
  if [ "$ARTNET_MODE" = "unicast" ] && [ -z "$ARTNET_TARGETS" ]; then
    die "ARTNET_MODE=unicast needs ARTNET_TARGETS."
  fi
  if [ "$AP_ENABLE" = "yes" ]; then
    echo "$AP_MAC" | grep -qiE '^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$' || die "AP_MAC '$AP_MAC' is not a MAC (AP profile must be MAC-pinned)."
    [ ${#AP_PSK} -ge 8 ] || die "AP_PSK must be 8+ characters (WPA2 minimum)."
  fi
  if [ "$WIFI_CLIENT" = "yes" ] && [ -z "$WIFI_SSID" ]; then die "WIFI_CLIENT=yes needs WIFI_SSID."; fi
  if [ "$SCREEN" = "headless" ] && [ "$KIOSK" = "yes" ]; then die "KIOSK=yes needs a screen."; fi
  if [ "$TUNNEL" = "yes" ] && [ -z "$TUNNEL_DOMAIN" ]; then die "TUNNEL=yes needs TUNNEL_DOMAIN."; fi
}
validate_conf

print_plan() {
  local at="(config.json untouched)"
  case "$ARTNET_MODE" in
    broadcast) at="broadcast $(derive_broadcast)";;
    unicast)   at="unicast → ${ARTNET_TARGETS}";;
  esac
  log "Install plan (${CONF_FILE})"
  cat <<PLAN
  Role         : ${ROLE}
  Hostname     : ${HOSTNAME_NEW:-<unchanged>}
  eth0         : ${ETH0_MODE}$( [ "$ETH0_MODE" = static ] && echo " ${ETH0_IP}/24 (never-default)" || true )
  Art-Net out  : ${at}
  WiFi client  : ${WIFI_CLIENT}$( [ "$WIFI_CLIENT" = yes ] && echo " → '${WIFI_SSID}'" || true )
  Access point : ${AP_ENABLE}$( [ "$AP_ENABLE" = yes ] && echo " → '${AP_SSID}' ${AP_BAND}/ch${AP_CHANNEL} @ ${AP_IP} pinned ${AP_MAC}" || true )
  Screen/kiosk : ${SCREEN} / kiosk=${KIOSK}
  Messenger    : ${MESSENGER}
  Tunnel       : ${TUNNEL}$( [ "$TUNNEL" = yes ] && echo " → ${TUNNEL_DOMAIN} ('${TUNNEL_NAME}')" || true )
PLAN
}
print_plan
if [ "$MODE_CHECK" = "yes" ]; then log "--check: config valid. Exiting."; exit 0; fi

if [ "$ASSUME_YES" != "yes" ]; then
  read -r -p $'\nProceed? [y/N] ' ans
  [[ "$ans" =~ ^[Yy]$ ]] || die "Aborted."
fi

# ── Preflight ───────────────────────────────────────────────────────────────
[ "$(id -un)" = "pi" ] || warn "Not running as 'pi'; paths assume /home/pi."
[ -f "${APP_DIR}/app.py" ] || die "app.py not found in ${APP_DIR} — run from the repo checkout."
[ -f "${APP_DIR}/config.json" ] || die "config.json not found in ${APP_DIR}."
command -v sudo >/dev/null || die "sudo not available."

# =============================================================================
# Modules — each idempotent, each gated by the conf
# =============================================================================

mod_packages() {
  log "Packages"
  sudo apt-get update -y
  sudo apt-get install -y python3-venv python3-pip git network-manager avahi-daemon iw whiptail
  if [ "$KIOSK" = "yes" ]; then
    sudo apt-get install -y lightdm openbox xserver-xorg xinit x11-xserver-utils unclutter
    # Chromium package name differs across Pi OS releases (Trixie: chromium).
    sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser || \
      warn "Could not install chromium; install it manually before the kiosk will work."
  fi
}

mod_lightboard() {
  log "Lightboard: venv + systemd unit"
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "${VENV}/bin/pip" install --upgrade pip
  "${VENV}/bin/pip" install -r "${APP_DIR}/requirements.txt"
  sudo tee /etc/systemd/system/lightboard.service >/dev/null <<UNIT
[Unit]
Description=Lightboard DMX controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=${APP_DIR}
ExecStart=${VENV}/bin/python ${APP_DIR}/app.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
}

mod_messenger() {
  [ "$MESSENGER" = "yes" ] || return 0
  log "Stage Messenger: clone/update + venv + systemd unit"
  if [ -d "${MSG_DIR}/.git" ]; then
    git -C "$MSG_DIR" pull --ff-only || warn "stage-messenger pull failed (continuing with current checkout)."
  else
    git clone "$MSG_REPO" "$MSG_DIR"
  fi
  [ -d "$MSG_VENV" ] || python3 -m venv "$MSG_VENV"
  "${MSG_VENV}/bin/pip" install --upgrade pip
  "${MSG_VENV}/bin/pip" install -r "${MSG_DIR}/requirements.txt"
  sudo tee /etc/systemd/system/stage-messenger.service >/dev/null <<UNIT
[Unit]
Description=Stage Messenger
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=${MSG_DIR}
Environment=PORT=3000
ExecStart=${MSG_VENV}/bin/python ${MSG_DIR}/server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
}

mod_tunnel() {
  [ "$TUNNEL" = "yes" ] || return 0
  log "Cloudflare tunnel (GUIDED)"
  if ! command -v cloudflared >/dev/null 2>&1; then
    sudo mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | \
      sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
    sudo apt-get update -y && sudo apt-get install -y cloudflared
  fi
  sudo mkdir -p /etc/cloudflared
  local creds_json=""
  if [ -n "$TUNNEL_BACKUP" ] && [ -f "$TUNNEL_BACKUP" ]; then
    log "Restoring tunnel creds from ${TUNNEL_BACKUP}"
    sudo tar -xzf "$TUNNEL_BACKUP" -C /etc/cloudflared
  fi
  creds_json=$(sudo sh -c 'ls /etc/cloudflared/*.json 2>/dev/null | head -1' || true)
  if [ -z "$creds_json" ]; then
    warn "No tunnel credentials found. One-time manual steps (interactive):"
    cat <<STEPS
    1. cloudflared tunnel login                 # opens a URL; auth in any browser
    2. cloudflared tunnel create ${TUNNEL_NAME} # writes ~/.cloudflared/<UUID>.json
    3. sudo cp ~/.cloudflared/*.json /etc/cloudflared/
    4. cloudflared tunnel route dns ${TUNNEL_NAME} ${TUNNEL_DOMAIN}
       cloudflared tunnel route dns ${TUNNEL_NAME} admin.${TUNNEL_DOMAIN}
    5. Re-run this installer (--config install.conf --yes) to finish.
STEPS
    warn "Tunnel config written with a placeholder — service NOT enabled until creds exist."
  fi
  local uuid="TUNNEL_UUID_HERE"
  [ -n "$creds_json" ] && uuid=$(basename "$creds_json" .json)
  local ingress_root=""
  [ "$MESSENGER" = "yes" ] && ingress_root="  - hostname: ${TUNNEL_DOMAIN}
    service: http://localhost:3000"
  sudo tee /etc/cloudflared/config.yml >/dev/null <<CFG
tunnel: ${uuid}
credentials-file: /etc/cloudflared/${uuid}.json

ingress:
${ingress_root}
  - hostname: admin.${TUNNEL_DOMAIN}
    service: http://localhost:5000
  - service: http_status:404
CFG
  if [ -n "$creds_json" ]; then
    sudo cloudflared service install 2>/dev/null || true
    sudo systemctl enable cloudflared
    echo "  Tunnel configured: ${TUNNEL_DOMAIN} (uuid ${uuid})"
    echo "  Reminder: gate admin.${TUNNEL_DOMAIN} with Cloudflare Access (Zero Trust) — see PI_INFRA.md."
  fi
}

mod_regdom() {
  # ch149 TX power is throttled until reg domain = US. `iw reg set` is NOT
  # persistent → set via raspi-config AND a boot oneshot before NetworkManager.
  [ "$AP_ENABLE" = "yes" ] || [ "$WIFI_CLIENT" = "yes" ] || return 0
  log "WiFi regulatory domain = US (persistent)"
  if command -v raspi-config >/dev/null 2>&1; then
    sudo raspi-config nonint do_wifi_country US || warn "raspi-config country set failed (non-fatal)."
  fi
  sudo tee /etc/systemd/system/wifi-regdom.service >/dev/null <<'UNIT'
[Unit]
Description=Set WiFi regulatory domain to US (unlocks ch149 TX power)
Before=NetworkManager.service
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'iw reg set US'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl enable wifi-regdom.service
}

mod_eth0() {
  # Profiles are created but not force-activated mid-script (don't drop an
  # eth0 SSH session during install); they come up on the final reboot.
  case "$ETH0_MODE" in
    static)
      log "eth0: static Art-Net LAN (${ETH0_IP}/24, never-default)"
      sudo nmcli con delete venue-artnet >/dev/null 2>&1 || true
      sudo nmcli con add type ethernet con-name venue-artnet ifname eth0 \
        ipv4.method manual ipv4.addresses "${ETH0_IP}/24" \
        ipv4.never-default yes ipv4.ignore-auto-dns yes \
        ipv6.method disabled \
        connection.autoconnect yes connection.autoconnect-priority 10
      ;;
    dhcp)
      log "eth0: DHCP 'mixer-network' (portable, never-default)"
      sudo nmcli con delete mixer-network >/dev/null 2>&1 || true
      sudo nmcli con add type ethernet con-name mixer-network ifname eth0 \
        ipv4.method auto ipv4.never-default yes ipv4.ignore-auto-dns yes \
        ipv6.method disabled \
        connection.autoconnect yes connection.autoconnect-priority 10
      ;;
    skip) log "eth0: left unconfigured";;
  esac
}

mod_wifi_client() {
  [ "$WIFI_CLIENT" = "yes" ] || return 0
  log "WiFi client profile: '${WIFI_SSID}'"
  # autoconnect-retries 0 = retry forever (hotspot boot-reconnect fix,
  # PI_INFRA.md 2026-06-28); priority 20 keeps wlan0 owning the default route.
  sudo nmcli con delete "wifi-${WIFI_SSID}" >/dev/null 2>&1 || true
  sudo nmcli con add type wifi con-name "wifi-${WIFI_SSID}" ifname wlan0 \
    802-11-wireless.ssid "${WIFI_SSID}" \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${WIFI_PSK}" \
    connection.autoconnect yes connection.autoconnect-priority 20 \
    connection.autoconnect-retries 0 \
    802-11-wireless.powersave 2
}

mod_ap() {
  [ "$AP_ENABLE" = "yes" ] || return 0
  log "Access point: '${AP_SSID}' ${AP_BAND}/ch${AP_CHANNEL} @ ${AP_IP} (pinned ${AP_MAC})"
  sudo nmcli con delete venue-ap >/dev/null 2>&1 || true
  sudo nmcli con add type wifi ifname '*' con-name venue-ap \
    802-11-wireless.ssid "${AP_SSID}" \
    802-11-wireless.mode ap \
    802-11-wireless.band "${AP_BAND}" \
    802-11-wireless.channel "${AP_CHANNEL}" \
    802-11-wireless.mac-address "${AP_MAC}" \
    wifi-sec.key-mgmt wpa-psk \
    802-11-wireless-security.proto rsn \
    802-11-wireless-security.pairwise ccmp \
    802-11-wireless-security.group ccmp \
    wifi-sec.psk "${AP_PSK}" \
    ipv4.method shared \
    ipv4.addresses "${AP_IP}/24" \
    connection.autoconnect yes
}

mod_screen() {
  [ "$SCREEN" = "headless" ] && { log "Screen: headless (nothing to do)"; return 0; }
  log "Screen: ${SCREEN}"
  [ -f "$BOOT_CFG" ] || die "$BOOT_CFG not found (expected on current Pi OS)."
  ensure_line() { grep -qxF "$1" "$BOOT_CFG" || echo "$1" | sudo tee -a "$BOOT_CFG" >/dev/null; }
  # KMS must be ON for any of these (fresh Pi OS default; un-comment if a
  # legacy driver script ever disabled it).
  sudo sed -i 's/^#\s*dtoverlay=vc4-kms-v3d/dtoverlay=vc4-kms-v3d/' "$BOOT_CFG"
  ensure_line "dtoverlay=vc4-kms-v3d"
  case "$SCREEN" in
    dsi_auto)
      # Official 7" + official-protocol clones (Hosyond 5" etc.) auto-detect;
      # just make sure auto-detect wasn't disabled.
      grep -qE '^display_auto_detect=1' "$BOOT_CFG" || ensure_line "display_auto_detect=1"
      ;;
    dsi_waveshare)
      # VERIFY the size tag against the exact panel model (7_0_inchC etc.).
      ensure_line "dtoverlay=vc4-kms-dsi-waveshare-panel,7_0_inchC"
      ;;
    hdmi)
      # EDID auto-negotiation — nothing to add. To force a mode for a stubborn
      # panel, append to /boot/firmware/cmdline.txt, e.g. video=HDMI-A-1:1024x600@60
      ;;
  esac
}

mod_kiosk() {
  [ "$KIOSK" = "yes" ] || return 0
  sudo systemctl set-default graphical.target
  log "Kiosk: lightdm autologin -> openbox -> chromium (${KIOSK_URL})"
  sudo mkdir -p /etc/lightdm/lightdm.conf.d
  sudo tee /etc/lightdm/lightdm.conf.d/50-lightboard-autologin.conf >/dev/null <<'CONF'
[Seat:*]
autologin-user=pi
autologin-user-timeout=0
user-session=openbox
CONF
  install -d -o pi -g pi /home/pi/.config/openbox
  tee /home/pi/.config/openbox/autostart >/dev/null <<KIOSKEOF
#!/bin/sh
# Lightboard kiosk — launched by the openbox session for user pi.
xset s off; xset -dpms; xset s noblank
unclutter -idle 0.1 -root &
# Rotated-panel touch hook (uncomment + set device/matrix if needed):
#   xinput set-prop "TOUCH_DEVICE_NAME" "Coordinate Transformation Matrix" 0 1 0 -1 0 1 0 0 1
if command -v chromium >/dev/null 2>&1; then CH=chromium; else CH=chromium-browser; fi
"\$CH" --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble \\
  --disable-features=TranslateUI --check-for-update-interval=31536000 \\
  --incognito "${KIOSK_URL}" &
KIOSKEOF
  chmod +x /home/pi/.config/openbox/autostart
  chown pi:pi /home/pi/.config/openbox/autostart
  sudo systemctl enable lightdm
}

mod_infra() {
  # device-timeout backstop (safe on KMS) + polkit rule so `pi` manages NM.
  # NOTE: no-dri-wait.conf is NEVER installed here — that was the legacy rack
  # SPI-screen fix and must not exist on a KMS display (BOOT_FIX.md).
  log "infra/ support files"
  sudo mkdir -p /etc/systemd/system.conf.d /etc/polkit-1/rules.d
  sudo cp "${APP_DIR}/infra/device-timeout.conf"    /etc/systemd/system.conf.d/device-timeout.conf
  sudo cp "${APP_DIR}/infra/50-lightboard-nm.rules" /etc/polkit-1/rules.d/50-lightboard-nm.rules
}

mod_config_json() {
  [ "$ARTNET_MODE" = "keep" ] && { log "config.json: untouched (keep)"; return 0; }
  local target
  if [ "$ARTNET_MODE" = "broadcast" ]; then target=$(derive_broadcast); else target="$ARTNET_TARGETS"; fi
  log "config.json: dmx_driver=artnet, artnet_target='${target}' (pinned skip-worktree)"
  python3 - "${APP_DIR}/config.json" "$target" <<'PYEOF'
import json, sys
path, target = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
cfg["dmx_driver"]    = "artnet"
cfg["artnet_target"] = target
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"  dmx_driver=artnet  artnet_target={target}")
PYEOF
  # Keep THIS machine's config.json local: git pull must never clobber it
  # (the repo's tracked config.json is effectively the original rack Pi's).
  # To pull a real upstream config change later: git update-index --no-skip-worktree config.json
  git -C "$APP_DIR" update-index --skip-worktree config.json && \
    echo "  config.json pinned (skip-worktree)."
}

mod_hostname() {
  [ -n "$HOSTNAME_NEW" ] || return 0
  [ "$HOSTNAME_NEW" = "$(hostname)" ] && return 0
  log "Hostname -> ${HOSTNAME_NEW}"
  sudo hostnamectl set-hostname "$HOSTNAME_NEW"
}

mod_enable() {
  log "Enabling services"
  sudo systemctl daemon-reload
  sudo systemctl enable avahi-daemon NetworkManager lightboard.service
  [ "$MESSENGER" = "yes" ] && sudo systemctl enable stage-messenger.service
  true
}

# =============================================================================
# Run
# =============================================================================
mod_packages
mod_lightboard
mod_messenger
mod_tunnel
mod_regdom
mod_eth0
mod_wifi_client
mod_ap
mod_screen
mod_kiosk
mod_infra
mod_config_json
mod_hostname
mod_enable

log "Install complete"
cat <<DONE
Reboot to bring everything up cleanly:

    sudo reboot

Verify after reboot:
    systemctl status lightboard --no-pager
$( [ "$MESSENGER" = "yes" ] && echo '    systemctl status stage-messenger --no-pager' )
$( [ "$ETH0_MODE" != "skip" ] && echo '    nmcli -g GENERAL.STATE,IP4.ADDRESS device show eth0' )
$( [ "$AP_ENABLE" = "yes" ] && echo "    nmcli con show --active | grep venue-ap    # then join '${AP_SSID}' and browse http://${AP_IP}:5000/touch" )
$( [ "$AP_ENABLE" = "yes" ] && echo '    iw reg get | grep country                  # country US' )

$( [ "$ARTNET_MODE" = "broadcast" ] && cat <<BC
Art-Net node setup (broadcast mode): put each node on the ${ETH0_IP%.*}.x
subnet (any host 1-254), set its port-address to the universe it should
output, Transmit = "Artnet -> dmx" (CR011R: hold Enter ~3s to commit).
No IP/MAC registration in Lightboard — nodes just need to be on-segment.
Add more output = configure another node's universe. CR011R blue LED
solid = Art-Net->DMX active.
BC
)
This machine re-installs reproducibly with:  ./install.sh --config install.conf --yes
DONE
