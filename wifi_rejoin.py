"""
WiFi rejoin watchdog -- whenever the WiFi client radio sits idle (NetworkManager device state
"disconnected"), look for a saved network in range and join it, whenever it shows up.

Why: NetworkManager tries a saved network 4 times (autoconnect-retries default) and then blocks it.
A phone or Nighthawk hotspot that starts broadcasting after the Pi has booted is then never picked
up (Presley's gig, 2026-11). Two layers:
  * tune()  -- every saved client WiFi profile with autoconnect on gets autoconnect-retries 0 (try
               forever) and powersave 2 (off). Re-run every TUNE_EVERY_S, because netplan/cloud-init
               profiles under /run are regenerated each boot and lose the setting.
  * tick()  -- wlan0 disconnected -> rescan (at most every RESCAN_EVERY_S), and if a saved profile
               with autoconnect on is in range, `nmcli connection up` it (highest
               autoconnect-priority first, then strongest signal). An explicit `up` ignores NM's
               retry block, so this works even on a profile that still has the default 4.

Left alone: wlan0 connected / connecting / unavailable (radio off) / unmanaged; device autoconnect
off (someone ran `nmcli device disconnect`); a radio an AP-mode profile is pinned to (MAC or ifname --
a USB AP dongle that re-enumerates under this name sits 'disconnected' for a moment); profiles with
autoconnect off (the WiFi page turns that off for a network it gave up on); AP-mode profiles; profiles
bound to another interface; any time the host's busy() says a WiFi-page connect is running. A profile whose `up` fails sits out FAIL_BACKOFF_S.

One process per box and radio acts: a flock on /run/lock/wifi-rejoin-<dev>.lock (fallback /tmp). A Pi
running both Lightboard and the mixer app gets one watchdog, whichever started first; the other takes
over if it stops.

Second radio (mixer netwifi/dualradio.py): a Rejoiner(dev2, bound_only=True,
respect_dev_autoconnect=False) brings up only profiles bound to the USB radio -- the uplink manager
keeps that device's NM autoconnect off on purpose, so the flag is ignored there.

SAME FILE in both repos -- keep them in step:
  mixer       netwifi/rejoin.py   (started by netwifi.init_netwifi)
  Lightboard  wifi_rejoin.py      (started by wifi_routes.register_wifi_routes)
Turn off with "wifi_rejoin": false in the app's config (mixer_config.json / config.json).
"""

import fcntl
import os
import subprocess
import threading
import time

TICK_S = 15
RESCAN_EVERY_S = 30
TUNE_EVERY_S = 300
FAIL_BACKOFF_S = 120
LOCK_PATHS = ("/run/lock/wifi-rejoin-{dev}.lock", "/tmp/wifi-rejoin-{dev}.lock")


def _run_default(args, timeout=20):
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


def _lead_int(s, default=None):
    """'0 (forever)' -> 0, '-1 (default)' -> -1, '2 (disable)' -> 2, '20' -> 20."""
    s = (s or "").strip().split(" ")[0]
    try:
        return int(s)
    except ValueError:
        return default


PROFILE_FIELDS = ("connection.autoconnect,connection.autoconnect-priority,connection.autoconnect-retries,"
                  "connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode,"
                  "802-11-wireless.powersave")
AP_FIELDS = "802-11-wireless.mode,802-11-wireless.mac-address,connection.interface-name"


class Rejoiner:
    def __init__(self, dev="wlan0", busy=None, on_join=None, run=None, log=None, clock=None,
                 bound_only=False, respect_dev_autoconnect=True):
        self.dev = dev
        self.bound_only = bound_only                    # only profiles with interface-name == dev
        self.respect_dev_autoconnect = respect_dev_autoconnect
        self.busy = busy or (lambda: False)
        self.on_join = on_join
        self.run = run or _run_default
        self.log = log or (lambda m: print("[wifi] " + m, flush=True))
        self.clock = clock or time.time
        self._last_rescan = 0.0
        self._last_tune = 0.0
        self._failed = {}            # profile name -> time its `up` failed
        self._lock_fd = None
        self._said_waiting = False   # "nothing in range" logged once per disconnect, not every tick
        self._said_ap = False
        self.last = {"action": None, "ts": 0.0, "detail": ""}

    # ── NetworkManager reads ────────────────────────────────────────────────
    def profiles(self, names=None):
        """Saved client WiFi profiles: [{name, ssid, autoconnect, priority, retries, iface, powersave}].
        names = only read these (skips the listing)."""
        if names is None:
            _, out, _ = self.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=10)
            names = [p[0] for p in (_split_terse(l) for l in out.splitlines())
                     if len(p) >= 2 and p[1] == "802-11-wireless" and p[0]]
        profs = []
        for name in names:
            rc, o, _ = self.run(["nmcli", "-t", "-f", PROFILE_FIELDS, "connection", "show", name], timeout=10)
            if rc != 0:
                continue
            f = {}
            for line in o.splitlines():
                parts = _split_terse(line)
                if len(parts) >= 2:
                    f[parts[0]] = ":".join(parts[1:]).strip()
            if f.get("802-11-wireless.mode") == "ap":
                continue
            profs.append({
                "name": name,
                "ssid": f.get("802-11-wireless.ssid") or name,
                "autoconnect": f.get("connection.autoconnect", "yes") == "yes",
                "priority": _lead_int(f.get("connection.autoconnect-priority"), 0),
                "retries": _lead_int(f.get("connection.autoconnect-retries"), -1),
                "iface": "" if f.get("connection.interface-name") in (None, "", "--")
                         else f.get("connection.interface-name"),
                "powersave": _lead_int(f.get("802-11-wireless.powersave"), 0),
            })
        return profs

    def dev_state(self):
        """'connected', 'disconnected', 'connecting (...)', 'unavailable', ... or None if no such device."""
        _, out, _ = self.run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"], timeout=10)
        for line in out.splitlines():
            p = _split_terse(line)
            if len(p) >= 2 and p[0] == self.dev:
                return p[1].strip()
        return None

    def is_ap_radio(self):
        """True if an AP-mode profile is pinned to this radio (by MAC or interface name). Such a radio
        sits 'disconnected' for a few seconds every time it re-enumerates (a flaky USB AP dongle) --
        joining a client network on it then would take the AP down for good."""
        _, out, _ = self.run(["nmcli", "-t", "-f", "GENERAL.HWADDR", "device", "show", self.dev], timeout=10)
        mac = ""
        for line in out.splitlines():
            p = _split_terse(line)
            if len(p) >= 2 and p[0] == "GENERAL.HWADDR":
                mac = ":".join(p[1:]).strip().upper()
        _, out, _ = self.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=10)
        names = [p[0] for p in (_split_terse(l) for l in out.splitlines())
                 if len(p) >= 2 and p[1] == "802-11-wireless" and p[0]]
        for name in names:
            rc, o, _ = self.run(["nmcli", "-t", "-f", AP_FIELDS, "connection", "show", name], timeout=10)
            if rc != 0:
                continue
            f = {}
            for line in o.splitlines():
                parts = _split_terse(line)
                if len(parts) >= 2:
                    f[parts[0]] = ":".join(parts[1:]).strip()
            if f.get("802-11-wireless.mode") != "ap":
                continue
            pin_mac = (f.get("802-11-wireless.mac-address") or "").upper()
            if (mac and pin_mac == mac) or f.get("connection.interface-name") == self.dev:
                return True
        return False

    def dev_autoconnect(self):
        """False only when NM says the device itself has autoconnect off (`nmcli device disconnect`)."""
        _, out, _ = self.run(["nmcli", "-t", "-f", "GENERAL.AUTOCONNECT", "device", "show", self.dev], timeout=10)
        for line in out.splitlines():
            p = _split_terse(line)
            if len(p) >= 2 and p[0] == "GENERAL.AUTOCONNECT":
                return p[1].strip() != "no"
        return True

    def visible(self, rescan):
        """{ssid: best signal} in range. rescan=True waits for a fresh scan (NM rate-limits; then it's the cache)."""
        _, out, _ = self.run(["nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list", "ifname", self.dev,
                              "--rescan", "yes" if rescan else "no"], timeout=25)
        seen = {}
        for line in out.splitlines():
            p = _split_terse(line)
            if len(p) < 2 or not p[0]:
                continue
            sig = _lead_int(p[1], 0)
            if sig > seen.get(p[0], -1):
                seen[p[0]] = sig
        return seen

    # ── layer 1: make NM itself keep trying ─────────────────────────────────
    def tune(self, only=None):
        """autoconnect-retries 0 + powersave 2 on saved client profiles with autoconnect on.
        only = a profile name (just joined from the WiFi page). Returns the names changed."""
        changed = []
        for p in self.profiles([only] if only else None):
            if not p["autoconnect"]:
                continue
            args = []
            if p["retries"] != 0:
                args += ["connection.autoconnect-retries", "0"]
            if p["powersave"] != 2:
                args += ["802-11-wireless.powersave", "2"]
            if not args:
                continue
            rc, _, err = self.run(["nmcli", "connection", "modify", p["name"]] + args, timeout=10)
            if rc == 0:
                changed.append(p["name"])
            else:
                self.log(f"rejoin: couldn't tune '{p['name']}': {(err or '').strip()[:120]}")
        if changed:
            self.log("rejoin: set retry-forever / power-save off on " + ", ".join(changed))
        if not only:
            self._last_tune = self.clock()
        return changed

    # ── layer 2: watchdog ───────────────────────────────────────────────────
    def _have_lock(self):
        if self._lock_fd is not None:
            return True
        for path in LOCK_PATHS:
            path = path.format(dev=self.dev)
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
            except OSError:
                try:                 # made by the other app under another user: flock works read-only too
                    fd = os.open(path, os.O_RDONLY)
                except OSError:
                    continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:          # another app on this box holds it
                os.close(fd)
                return False
            self._lock_fd = fd
            return True
        return True                  # nowhere to put a lock: act anyway

    def _note(self, action, detail):
        self.last = {"action": action, "ts": self.clock(), "detail": detail}

    def tick(self):
        """One check. Returns what happened: 'not_owner' | 'busy' | 'skip:<state>' | 'dev_off' |
        'ap_radio' | 'nothing' | 'joined:<profile>' | 'failed:<profile>'."""
        if not self._have_lock():
            return "not_owner"
        now = self.clock()
        if now - self._last_tune >= TUNE_EVERY_S:
            self.tune()
        st = self.dev_state()
        if st != "disconnected":
            self._said_waiting = False
            return "skip:" + str(st)
        if self.busy():
            return "busy"
        if self.respect_dev_autoconnect and not self.dev_autoconnect():
            return "dev_off"
        if self.is_ap_radio():
            if not self._said_ap:
                self.log(f"rejoin: {self.dev} is the AP radio -- leaving it alone")
                self._said_ap = True
            return "ap_radio"
        rescan = now - self._last_rescan >= RESCAN_EVERY_S
        if rescan:
            self._last_rescan = now
        seen = self.visible(rescan)
        cands = [p for p in self.profiles()
                 if p["autoconnect"] and p["iface"] in ((self.dev,) if self.bound_only else ("", self.dev))
                 and p["ssid"] in seen
                 and now - self._failed.get(p["name"], -1e9) >= FAIL_BACKOFF_S]
        if not cands:
            if not self._said_waiting:
                self.log(f"rejoin: {self.dev} disconnected, no saved network in range yet -- watching")
                self._said_waiting = True
            self._note("waiting", "no saved network in range")
            return "nothing"
        cands.sort(key=lambda p: (-p["priority"], -seen[p["ssid"]]))
        p = cands[0]
        if self.busy():              # the WiFi page started a connect during the scan
            return "busy"
        self.log(f"rejoin: {self.dev} disconnected, '{p['ssid']}' in range -- connecting ({p['name']})")
        rc, out, err = self.run(["nmcli", "connection", "up", p["name"], "ifname", self.dev], timeout=45)
        if rc == 0:
            self._failed.pop(p["name"], None)
            self._said_waiting = False
            self.log(f"rejoin: connected to '{p['ssid']}'")
            self._note("joined", p["ssid"])
            if self.on_join:
                try:
                    self.on_join(p["name"])
                except Exception:  # noqa: BLE001
                    pass
            return "joined:" + p["name"]
        self._failed[p["name"]] = now
        msg = ((err or out or "").strip().splitlines() or ["failed"])[-1][:160]
        self.log(f"rejoin: '{p['ssid']}' didn't come up ({msg}); retry in {FAIL_BACKOFF_S}s")
        self._note("failed", p["ssid"] + ": " + msg)
        return "failed:" + p["name"]

    def _loop(self):
        time.sleep(5)                # let NM's own boot-time autoconnect go first
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.log(f"rejoin: tick error {e}")
            time.sleep(TICK_S)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="wifi-rejoin").start()
        return self
