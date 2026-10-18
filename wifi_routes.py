# wifi_routes.py — Venue WiFi management for Lightboard
#
# Adds a /wifi page (admin-only, behind your Cloudflare Access gate on :5000)
# that scans, connects, and saves WiFi networks via NetworkManager.
#
# Wire into app.py with two lines:
#   from wifi_routes import register_wifi_routes      # near the other imports
#   register_wifi_routes(app)                          # right after app = Flask(__name__)
#
# Requires a polkit rule letting the service user (pi) manage NetworkManager
# (see 50-lightboard-nm.rules). nmcli auto-saves connected profiles with
# autoconnect on, so returning to a venue reconnects on its own.

import subprocess
import threading
import time
import urllib.request
from flask import render_template, request, jsonify

WIFI_DEV = "wlan0"
ETH_DEV = "eth0"
ETH_PROFILE = "mixer-network"   # the DHCP eth0 profile the mixer install makes (console LAN only)
KIOSK_CMD_FILE = "/tmp/lightboard_kiosk_cmd"   # watched by kiosk_portal_watch.sh in the GUI session
PORTAL_TRIGGER_URL = "http://neverssl.com"     # plain-HTTP URL that trips the captive-portal redirect

# Shared state for the async connect operation. The connect runs in a thread
# because switching networks can drop the very request that started it (when
# you're driving this through the tunnel) — the browser then polls /status.
_connect_state = {"status": "idle", "ssid": None, "detail": "", "ts": 0.0}
_state_lock = threading.Lock()
_scan_sec = {}                       # ssid -> nmcli SECURITY string from the last scan


def _run(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _split_terse(line):
    """Split an `nmcli -t` line on unescaped ':' (nmcli escapes ':' as '\\:')."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    out.append(cur)
    return out


def _set_state(status, ssid=None, detail=""):
    with _state_lock:
        _connect_state.update(status=status, ssid=ssid, detail=detail, ts=time.time())


def _kiosk_cmd(value):
    """Signal the in-session kiosk watcher: a URL to display, or 'restore' the touch kiosk."""
    try:
        with open(KIOSK_CMD_FILE, "w") as f:
            f.write(value)
    except Exception:  # noqa: BLE001
        pass


def scan_networks(rescan=True):
    mode = "yes" if rescan else "auto"
    _, out, _ = _run(
        ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
         "device", "wifi", "list", "ifname", WIFI_DEV, "--rescan", mode], timeout=25)
    nets = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = _split_terse(line)
        if len(parts) < 4:
            continue
        in_use, ssid, signal, security = parts[0], parts[1], parts[2], parts[3]
        if not ssid:  # hidden network
            continue
        try:
            sig = int(signal)
        except ValueError:
            sig = 0
        sec = security if security not in ("", "--") else ""
        entry = {
            "ssid": ssid,
            "signal": sig,
            "secure": bool(sec),
            "security": sec,
            "in_use": in_use.strip() == "*",
        }
        cur = nets.get(ssid)
        if cur is None or entry["in_use"] or sig > cur["signal"]:
            nets[ssid] = entry
    _scan_sec.update({n["ssid"]: n["security"] for n in nets.values()})
    saved = {}
    for p in saved_wifi_profiles():
        saved.setdefault(p["ssid"], p["name"])
    for n in nets.values():
        n["saved"] = saved.get(n["ssid"])
    return sorted(nets.values(), key=lambda n: (0 if n["in_use"] else 1, -n["signal"]))


def _active_wlan_connection():
    _, out, _ = _run(["nmcli", "-t", "-f", "GENERAL.CONNECTION",
                      "device", "show", WIFI_DEV])
    for line in out.splitlines():
        if line.startswith("GENERAL.CONNECTION:"):
            name = line.split(":", 1)[1].strip()
            return name if name and name != "--" else None
    return None


def saved_wifi_profiles():
    """Saved client WiFi profiles as [{"name", "ssid"}]. The profile name can differ from the SSID
    (Imager's 'preconfigured'), so read the real SSID. AP-mode profiles (venue-ap) are left out --
    they must never be joined as a client or shown with a Forget button."""
    _, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    names = [p[0] for p in (_split_terse(l) for l in out.splitlines())
             if len(p) >= 2 and p[1] == "802-11-wireless" and p[0]]
    profs = []
    for name in names:
        _, o, _ = _run(["nmcli", "-t", "-f", "802-11-wireless.ssid,802-11-wireless.mode",
                        "connection", "show", name], timeout=10)
        ssid, mode = name, ""
        for line in o.splitlines():
            parts = _split_terse(line)
            key, val = parts[0], ":".join(parts[1:]).strip()
            if key == "802-11-wireless.ssid" and val:
                ssid = val
            elif key == "802-11-wireless.mode":
                mode = val
        if mode == "ap":
            continue
        profs.append({"name": name, "ssid": ssid})
    return profs


def _profile_for_ssid(ssid):
    for p in saved_wifi_profiles():
        if p["ssid"] == ssid:
            return p["name"]
    return None


def _join(ssid, password, profile=None):
    """Join ssid. A saved profile is brought up as-is (no password needed); a password given for a
    saved profile replaces its stored one first. Otherwise a new profile is created.
    Returns (rc, out, err, conn, created): conn = the NM profile name now in use; created = this
    call made a new profile (only those get deleted again if the join fails)."""
    prof = profile or _profile_for_ssid(ssid)
    if prof:
        if password:
            rc, out, err = _run(["nmcli", "connection", "modify", prof, "wifi-sec.psk", password], timeout=10)
            if rc != 0:
                return rc, out, err, prof, False
        rc, out, err = _run(["nmcli", "connection", "up", prof, "ifname", WIFI_DEV], timeout=45)
        return rc, out, err, prof, False
    args = ["nmcli", "device", "wifi", "connect", ssid, "ifname", WIFI_DEV]
    if password:
        args += ["password", password]
    rc, out, err = _run(args, timeout=45)
    return rc, out, err, ssid, True


def _secrets_error(out, err):
    t = ((err or "") + " " + (out or "")).lower()
    return "secrets were required" in t or "802-11-wireless-security" in t or "no secrets" in t


def _join_retry(conn, created, security=""):
    """Second try after 'Secrets were required, but not provided'. NM reports a 4-way-handshake
    timeout the same way as a wrong password, and on the Pi's brcmfmac the first association to a
    WPA2/WPA3 mixed network often times out (SAE first), then works on the next try. The profile NM
    made on the failed try is still there, so bring it up; on a mixed network pin it to WPA2-PSK
    first so the retry doesn't go down the SAE path again. Returns (rc, out, err)."""
    if created and "WPA3" in (security or "") and "WPA2" in (security or ""):
        _run(["nmcli", "connection", "modify", conn, "wifi-sec.key-mgmt", "wpa-psk"], timeout=10)
    time.sleep(3)
    return _run(["nmcli", "connection", "up", conn, "ifname", WIFI_DEV], timeout=45)


def _keep(conn):
    """Joined with internet: make sure it auto-reconnects (an earlier no-internet revert turns that off)."""
    _run(["nmcli", "connection", "modify", conn, "connection.autoconnect", "yes"], timeout=10)


def _wlan_ip():
    _, out, _ = _run(["nmcli", "-t", "-f", "IP4.ADDRESS",
                      "device", "show", WIFI_DEV])
    for line in out.splitlines():
        if line.startswith("IP4.ADDRESS"):
            val = line.split(":", 1)[1].strip()
            return val.split("/")[0] if val else None
    return None


def _nm_connectivity():
    _, out, _ = _run(["nmcli", "networking", "connectivity", "check"], timeout=12)
    return (out.strip() or "unknown")


def _internet_ok():
    """True only if we get a clean 204 (no portal, real internet)."""
    try:
        req = urllib.request.Request(
            "http://connectivitycheck.gstatic.com/generate_204",
            headers={"User-Agent": "lightboard-wifi"})
        resp = urllib.request.urlopen(req, timeout=5)
        return getattr(resp, "status", resp.getcode()) == 204
    except Exception:  # noqa: BLE001
        return False


# ── Internet source: WiFi (default) or Ethernet ────────────────────────────────
# Only one thing decides where internet traffic goes: which interface owns the default route.
# NetworkManager gives Ethernet a better metric (100) than WiFi (600), so an eth0 profile that
# accepts the mixer router's gateway silently takes all internet traffic -- to a router with no
# internet. "WiFi" = the eth0 profile has ipv4.never-default (and ignores the router's DNS);
# "Ethernet" = it takes the gateway (WiFi stays as fallback, by metric). The choice lives in the
# NM profile itself. Same code as the mixer repo's netwifi (keep them in step).

def _default_route():
    """Best IPv4 default route: {"dev", "via", "metric"} (dev None when there is none)."""
    _, out, _ = _run(["ip", "-4", "route", "show", "default"])
    routes = []
    for line in out.splitlines():
        w = line.split()
        if not w or w[0] != "default":
            continue
        r = {"dev": None, "via": None, "metric": 0}
        for i, t in enumerate(w):
            if t == "dev" and i + 1 < len(w):
                r["dev"] = w[i + 1]
            elif t == "via" and i + 1 < len(w):
                r["via"] = w[i + 1]
            elif t == "metric" and i + 1 < len(w):
                try:
                    r["metric"] = int(w[i + 1])
                except ValueError:
                    pass
        routes.append(r)
    routes.sort(key=lambda r: r["metric"])
    return routes[0] if routes else {"dev": None, "via": None, "metric": None}


def _eth_profiles():
    """Ethernet profiles as [{"name", "iface", "active", "method", "never_default", "ephemeral"}].
    `ephemeral` = made by netplan / cloud-init under /run (regenerated every boot)."""
    _, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE,FILENAME", "connection", "show"])
    profs = []
    for line in out.splitlines():
        p = _split_terse(line)
        if len(p) < 3 or p[1] != "802-3-ethernet" or not p[0]:
            continue
        fn = p[3] if len(p) > 3 else ""
        _, o, _ = _run(["nmcli", "-t", "-f", "connection.interface-name,ipv4.method,ipv4.never-default",
                        "connection", "show", p[0]], timeout=10)
        iface, method, nd = "", "auto", "no"
        for l in o.splitlines():
            q = _split_terse(l)
            k, v = q[0], ":".join(q[1:]).strip()
            if k == "connection.interface-name":
                iface = v if v != "--" else ""
            elif k == "ipv4.method":
                method = v
            elif k == "ipv4.never-default":
                nd = v
        profs.append({"name": p[0], "iface": iface, "active": p[2] == ETH_DEV, "method": method,
                      "never_default": nd == "yes",
                      "ephemeral": fn.startswith("/run/") or p[0].startswith("netplan-")})
    return profs


def _eth_target(profs):
    """The eth0 profile the selector edits: the active one, else one bound to eth0, else any. An
    ephemeral (netplan) one can't hold the setting, so then it's `mixer-network` (made if missing)."""
    cands = [p for p in profs if p["active"]] or [p for p in profs if p["iface"] == ETH_DEV] or profs
    mine = next((p for p in profs if p["name"] == ETH_PROFILE), None)
    if cands and not cands[0]["ephemeral"]:
        return cands[0]
    return mine


def internet_source():
    """{"mode": "wifi"|"ethernet"|"unknown", "eth_profile", "route_dev", "route_via", "effective"}."""
    rt = _default_route()
    profs = _eth_profiles()
    t = _eth_target(profs)
    if t is None:
        t = next((p for p in profs if p["active"]), None) or (profs[0] if profs else None)
    mode = "unknown" if t is None else ("wifi" if t["never_default"] else "ethernet")
    return {"mode": mode, "eth_profile": t["name"] if t else None,
            "route_dev": rt["dev"], "route_via": rt["via"],
            "effective": "ethernet" if rt["dev"] == ETH_DEV else "wifi" if rt["dev"] == WIFI_DEV else
                         "none" if not rt["dev"] else rt["dev"]}


def set_internet_source(mode):
    """Make `mode` ("wifi" | "ethernet") the box's internet route. Returns (ok, message, profile)."""
    if mode not in ("wifi", "ethernet"):
        return False, "mode must be wifi or ethernet", None
    nd = "yes" if mode == "wifi" else "no"
    profs = _eth_profiles()
    t = _eth_target(profs)
    if t is None:
        if any(p["method"] == "manual" for p in profs):
            return False, "eth0 has a static profile that can't be changed from here", None
        rc, _, err = _run(["nmcli", "connection", "add", "type", "ethernet", "con-name", ETH_PROFILE,
                           "ifname", ETH_DEV, "ipv4.method", "auto", "ipv4.never-default", nd,
                           "ipv4.ignore-auto-dns", nd, "ipv6.method", "disabled",
                           "connection.autoconnect", "yes", "connection.autoconnect-priority", "10"], timeout=15)
        if rc != 0:
            return False, "couldn't make the " + ETH_PROFILE + " profile: " + (err or "").strip(), None
        name, made = ETH_PROFILE, True
    else:
        name, made = t["name"], False
        rc, _, err = _run(["nmcli", "connection", "modify", name, "ipv4.never-default", nd,
                           "ipv4.ignore-auto-dns", nd], timeout=10)
        if rc != 0:
            return False, "couldn't change " + name + ": " + (err or "").strip(), name
    for p in profs:
        if p["name"] != name and p["ephemeral"]:
            _run(["nmcli", "connection", "modify", p["name"], "ipv4.never-default", nd,
                  "ipv4.ignore-auto-dns", nd], timeout=10)
    active = next((p for p in profs if p["active"]), None)
    if made or (active and active["name"] != name) or not active:
        rc, _, err = _run(["nmcli", "connection", "up", name], timeout=40)
    else:
        rc, _, err = _run(["nmcli", "device", "reapply", ETH_DEV], timeout=20)   # same profile: no link drop
        if rc != 0:
            rc, _, err = _run(["nmcli", "connection", "up", name], timeout=40)
    if rc != 0:
        return False, "changed " + name + " but couldn't apply it: " + (err or "").strip(), name
    return True, ("internet via WiFi (eth0 stays console/Art-Net only)" if mode == "wifi"
                  else "internet via Ethernet (router on eth0); WiFi is the fallback"), name


def get_status():
    with _state_lock:
        connect = dict(_connect_state)
    return {
        "ssid": _active_wlan_connection(),
        "ip": _wlan_ip(),
        "connectivity": _nm_connectivity(),
        "internet": internet_source(),
        "connect": connect,
    }


def _do_connect(ssid, password, prev, profile=None):
    _set_state("connecting", ssid, "Associating with " + ssid)
    rc, out, err, conn, created = _join(ssid, password, profile)
    retried = False
    if rc != 0 and _secrets_error(out, err):          # handshake timeout looks like a bad password: once more
        _set_state("connecting", ssid, "First try didn't take (handshake) \u2014 trying " + ssid + " again")
        rc, out, err = _join_retry(conn, created, _scan_sec.get(ssid, ""))
        retried = True

    if rc != 0:
        msg = (err or out or "Could not connect").strip().splitlines()
        msg = (msg[-1] if msg else "Could not connect")[:200]
        if retried:
            msg += " (tried twice \u2014 wrong password?)"
        _set_state("failed", ssid, msg)
        if created:                          # drop a half-made profile -- never a saved one
            _run(["nmcli", "connection", "delete", conn], timeout=10)
        if prev and prev != conn:
            _run(["nmcli", "connection", "up", prev], timeout=30)
        return

    _set_state("checking", ssid, "Connected — checking for internet")
    ok = False
    for _ in range(6):          # ~18s
        time.sleep(3)
        if _internet_ok():
            ok = True
            break
    if ok:
        _keep(conn)
        _set_state("success", ssid, "Connected to " + ssid + " with internet")
        return

    # No real internet (portal / limited / none) — don't strand: revert.
    label = _nm_connectivity()
    detail = {
        "portal":  "Captive portal detected — no open internet.",
        "limited": "Connected but no internet (limited).",
        "none":    "Connected but no internet.",
    }.get(label, "No internet after connecting.")
    _run(["nmcli", "connection", "modify", conn, "connection.autoconnect", "no"], timeout=10)
    if prev and prev != conn:
        _run(["nmcli", "connection", "up", prev], timeout=30)
    _set_state("no_internet", ssid, detail + " Reverted to your previous network.")


def _do_portal_login(ssid, password, prev, profile=None):
    """Connect, then pop the captive portal onto the Pi's touchscreen and wait for
    the user to complete it. Restores the touch kiosk and keeps/reverts accordingly."""
    _set_state("connecting", ssid, "Connecting to " + ssid)
    rc, out, err, conn, created = _join(ssid, password, profile)
    if rc != 0 and _secrets_error(out, err):
        rc, out, err = _join_retry(conn, created, _scan_sec.get(ssid, ""))
    if rc != 0:
        msg = (err or out or "Could not connect").strip().splitlines()
        _set_state("failed", ssid, (msg[-1] if msg else "Could not connect")[:200])
        if created:
            _run(["nmcli", "connection", "delete", conn], timeout=10)
        if prev and prev != conn:
            _run(["nmcli", "connection", "up", prev], timeout=30)
        return

    # On the venue network now (no internet yet). Show the portal on the Pi screen.
    _kiosk_cmd(PORTAL_TRIGGER_URL)
    _set_state("portal", ssid, "Tap through the venue's login on the Pi's touchscreen.")
    ok = False
    for _ in range(60):                 # up to ~3 min to complete the portal
        time.sleep(3)
        if _internet_ok():
            ok = True
            break
    _kiosk_cmd("restore")               # always put the touch kiosk back

    if ok:
        _keep(conn)
        _set_state("success", ssid, "Portal complete — " + ssid + " is connected with internet.")
        return
    _run(["nmcli", "connection", "modify", conn, "connection.autoconnect", "no"], timeout=10)
    if prev and prev != conn:
        _run(["nmcli", "connection", "up", prev], timeout=30)
    _set_state("portal_timeout", ssid, "Portal not completed in time — reverted to your previous network.")


def register_wifi_routes(app):
    @app.route("/wifi")
    def wifi_page():
        return render_template("wifi.html")

    @app.route("/api/wifi/scan")
    def api_wifi_scan():
        rescan = request.args.get("rescan", "1") != "0"
        return jsonify({"networks": scan_networks(rescan)})

    @app.route("/api/wifi/status")
    def api_wifi_status():
        return jsonify(get_status())

    @app.route("/api/wifi/connect", methods=["POST"])
    def api_wifi_connect():
        data = request.get_json(silent=True) or {}
        ssid = (data.get("ssid") or "").strip()
        password = data.get("password") or ""
        profile = (data.get("profile") or "").strip() or None
        if not ssid:
            return jsonify({"ok": False, "error": "SSID required"}), 400
        if profile and profile not in {p["name"] for p in saved_wifi_profiles()}:
            return jsonify({"ok": False, "error": "No saved network named " + profile}), 400
        with _state_lock:
            if _connect_state["status"] in ("connecting", "checking"):
                return jsonify({"ok": False, "error": "A connect attempt is already running"}), 409
            _connect_state.update(status="connecting", ssid=ssid, detail="Starting", ts=time.time())
        prev = _active_wlan_connection()
        threading.Thread(target=_do_connect, args=(ssid, password, prev, profile), daemon=True).start()
        return jsonify({"ok": True, "started": True})

    @app.route("/api/wifi/portal-login", methods=["POST"])
    def api_wifi_portal_login():
        data = request.get_json(silent=True) or {}
        ssid = (data.get("ssid") or "").strip()
        password = data.get("password") or ""
        profile = (data.get("profile") or "").strip() or None
        if not ssid:
            return jsonify({"ok": False, "error": "SSID required"}), 400
        if profile and profile not in {p["name"] for p in saved_wifi_profiles()}:
            return jsonify({"ok": False, "error": "No saved network named " + profile}), 400
        with _state_lock:
            if _connect_state["status"] in ("connecting", "checking", "portal"):
                return jsonify({"ok": False, "error": "Busy"}), 409
            _connect_state.update(status="connecting", ssid=ssid, detail="Starting", ts=time.time())
        prev = _active_wlan_connection()
        threading.Thread(target=_do_portal_login, args=(ssid, password, prev, profile), daemon=True).start()
        return jsonify({"ok": True, "started": True})

    @app.route("/api/wifi/internet-source", methods=["POST"])
    def api_wifi_internet_source():
        """{"mode": "wifi"|"ethernet"}: which connection carries the Pi's internet traffic."""
        mode = (request.get_json(silent=True) or {}).get("mode")
        ok, msg, prof = set_internet_source(mode)
        print(f"[wifi] internet source -> {mode}: {msg} [{prof}]", flush=True)
        return jsonify({"ok": ok, "error": "" if ok else msg, "message": msg, "internet": internet_source()}), \
            (200 if ok else 500)

    @app.route("/api/wifi/saved")
    def api_wifi_saved():
        profs = saved_wifi_profiles()
        return jsonify({"saved": [p["name"] for p in profs], "profiles": profs,
                        "active": _active_wlan_connection()})

    @app.route("/api/wifi/forget", methods=["POST"])
    def api_wifi_forget():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        if name not in {p["name"] for p in saved_wifi_profiles()}:   # never the AP profile
            return jsonify({"ok": False, "error": "not a saved client network"}), 400
        rc, _, err = _run(["nmcli", "connection", "delete", name], timeout=15)
        return jsonify({"ok": rc == 0, "error": (err or "").strip()})
