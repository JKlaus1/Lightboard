#!/usr/bin/env python3
"""
Off-hardware tests for the WiFi rejoin watchdog (netwifi/rejoin.py in the mixer repo, wifi_rejoin.py in
Lightboard -- same file). NetworkManager is faked; nothing touches the network.

  python3 -m netwifi.tests.test_rejoin      (mixer repo root)
  python3 test_wifi_rejoin.py               (Lightboard repo root)
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from netwifi import rejoin as R          # mixer
except ImportError:
    import wifi_rejoin as R                  # Lightboard

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeNM:
    """Just enough nmcli for rejoin.py."""
    def __init__(self):
        self.profiles = {}           # name -> dict of nmcli fields
        self.state = "disconnected"
        self.dev_auto = "yes"
        self.visible = {}            # ssid -> signal
        self.up_rc = {}              # profile -> rc for `connection up`
        self.calls = []
        self.rescans = 0

    def add(self, name, ssid=None, auto="yes", prio=0, retries="-1 (default)", iface="", mode="infrastructure",
            ps="0 (default)"):
        self.profiles[name] = {"connection.autoconnect": auto, "connection.autoconnect-priority": str(prio),
                               "connection.autoconnect-retries": retries, "connection.interface-name": iface,
                               "802-11-wireless.ssid": ssid or name, "802-11-wireless.mode": mode,
                               "802-11-wireless.powersave": ps}

    @staticmethod
    def esc(s):
        return s.replace("\\", "\\\\").replace(":", "\\:")

    def __call__(self, args, timeout=20):
        self.calls.append(list(args))
        a = list(args)
        if a == ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"]:
            out = [f"{self.esc(n)}:802-11-wireless" for n in self.profiles] + ["mixer-network:802-3-ethernet"]
            return 0, "\n".join(out) + "\n", ""
        if a[:4] == ["nmcli", "-t", "-f", R.PROFILE_FIELDS] and a[4:6] == ["connection", "show"]:
            p = self.profiles.get(a[6])
            if p is None:
                return 10, "", "no such connection"
            return 0, "".join(f"{k}:{self.esc(v)}\n" for k, v in p.items()), ""
        if a == ["nmcli", "-t", "-f", "DEVICE,STATE", "device"]:
            wl = f"wlan0:{self.state}\n" if self.state is not None else ""
            return 0, "eth0:connected\n" + wl + "lo:connected (externally)\n", ""
        if a[:5] == ["nmcli", "-t", "-f", "GENERAL.AUTOCONNECT", "device"]:
            return 0, f"GENERAL.AUTOCONNECT:{self.dev_auto}\n", ""
        if a[:6] == ["nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi"]:
            if a[-1] == "yes":
                self.rescans += 1
            return 0, "".join(f"{self.esc(s)}:{g}\n" for s, g in self.visible.items()) + ":40\n", ""
        if a[:3] == ["nmcli", "connection", "modify"]:
            p = self.profiles[a[3]]
            kv = a[4:]
            for k, v in zip(kv[::2], kv[1::2]):
                p[k] = v
            return 0, "", ""
        if a[:3] == ["nmcli", "connection", "up"]:
            rc = self.up_rc.get(a[3], 0)
            if rc == 0:
                self.state = "connected"
            return rc, "", "" if rc == 0 else "Error: Connection activation failed: Secrets were required"
        return 0, "", ""

    def ups(self):
        return [c[3] for c in self.calls if c[:3] == ["nmcli", "connection", "up"]]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(nm, busy=False, joined=None, shared_lock=None):
    """A Rejoiner on the fake NM. Each gets its own lock file unless shared_lock is given (the
    one-watchdog-per-box test), so earlier instances in this process don't block later ones."""
    R.LOCK_PATHS = (shared_lock or os.path.join(tempfile.mkdtemp(), "wifi-rejoin.lock"),)
    clk = Clock()
    flag = {"busy": busy}
    r = R.Rejoiner("wlan0", busy=lambda: flag["busy"], run=nm, log=lambda m: None, clock=clk,
                   on_join=(lambda n: joined.append(n)) if joined is not None else None)
    return r, clk, flag


def main():
    print("tuning")
    nm = FakeNM()
    nm.add("ChocolateSalty")
    nm.add("netplan-wlan0-Home", ssid="Home", retries="0 (forever)", ps="2 (disable)")
    nm.add("Bad Venue", auto="no")
    nm.add("venue-ap", ssid="Lights-Rig", mode="ap")
    r, clk, _ = make(nm)
    ch = r.tune()
    check("page-joined profile gets retries 0 + powersave 2", ch == ["ChocolateSalty"]
          and nm.profiles["ChocolateSalty"]["connection.autoconnect-retries"] == "0"
          and nm.profiles["ChocolateSalty"]["802-11-wireless.powersave"] == "2", str(ch))
    check("already-tuned profile not touched", "netplan-wlan0-Home" not in ch)
    check("autoconnect-off profile not touched", nm.profiles["Bad Venue"]["connection.autoconnect-retries"] == "-1 (default)")
    check("AP profile not touched", nm.profiles["venue-ap"]["connection.autoconnect-retries"] == "-1 (default)")
    check("second tune changes nothing", r.tune() == [])
    nm.profiles["netplan-wlan0-Home"]["connection.autoconnect-retries"] = "-1 (default)"   # reboot regenerated it
    check("tune(only=) touches just that profile", r.tune(only="netplan-wlan0-Home") == ["netplan-wlan0-Home"])

    print("hotspot comes up late (Presley's gig)")
    nm = FakeNM()
    nm.add("ChocolateSalty", prio=0)
    joined = []
    r, clk, _ = make(nm, joined=joined)
    check("nothing in range -> waits", r.tick() == "nothing" and nm.ups() == [])
    check("first tick rescanned", nm.rescans == 1)
    clk.t += 15
    r.tick()
    check("no second rescan inside 30 s", nm.rescans == 1)
    clk.t += 15
    nm.visible = {"ChocolateSalty": 70}
    res = r.tick()
    check("hotspot appears -> joined", res == "joined:ChocolateSalty" and nm.ups() == ["ChocolateSalty"], res)
    check("on_join called", joined == ["ChocolateSalty"])
    check("last action recorded", r.last["action"] == "joined" and r.last["detail"] == "ChocolateSalty")
    clk.t += 15
    check("connected -> left alone", r.tick() == "skip:connected" and nm.ups() == ["ChocolateSalty"])

    print("guards")
    for st in ("connecting (getting IP configuration)", "unavailable", "unmanaged"):
        nm = FakeNM(); nm.add("A"); nm.visible = {"A": 50}; nm.state = st
        r, _, _ = make(nm)
        check(f"state '{st}' -> no action", r.tick().startswith("skip:") and nm.ups() == [])
    nm = FakeNM(); nm.add("A"); nm.visible = {"A": 50}; nm.state = None
    r, _, _ = make(nm)
    check("no wlan0 at all -> no action", r.tick() == "skip:None" and nm.ups() == [])
    nm = FakeNM(); nm.add("A"); nm.visible = {"A": 50}
    r, _, flag = make(nm, busy=True)
    check("WiFi-page connect running -> busy, no join", r.tick() == "busy" and nm.ups() == [])
    flag["busy"] = False
    check("page done -> joins", r.tick() == "joined:A")
    nm = FakeNM(); nm.add("A"); nm.visible = {"A": 50}; nm.dev_auto = "no"
    r, _, _ = make(nm)
    check("`nmcli device disconnect` (device autoconnect off) respected", r.tick() == "dev_off" and nm.ups() == [])
    nm = FakeNM(); nm.add("Gave Up On", auto="no"); nm.visible = {"Gave Up On": 90}
    r, _, _ = make(nm)
    check("profile with autoconnect off never joined", r.tick() == "nothing" and nm.ups() == [])
    nm = FakeNM(); nm.add("Rig", ssid="Lights-Rig", mode="ap"); nm.visible = {"Lights-Rig": 90}
    r, _, _ = make(nm)
    check("AP profile never joined as client", r.tick() == "nothing" and nm.ups() == [])
    nm = FakeNM(); nm.add("Dongle Net", iface="wlan1"); nm.visible = {"Dongle Net": 90}
    r, _, _ = make(nm)
    check("profile bound to another interface skipped", r.tick() == "nothing" and nm.ups() == [])
    nm = FakeNM(); nm.visible = {"Somebody Else": 90}
    r, _, _ = make(nm)
    check("no saved networks -> nothing", r.tick() == "nothing" and nm.ups() == [])

    print("choice")
    nm = FakeNM()
    nm.add("Home", prio=0)
    nm.add("android-hotspot", ssid="Pixel", prio=20)
    nm.add("venue-link", ssid="Lights-Rig", prio=5)
    nm.visible = {"Home": 90, "Pixel": 30, "Lights-Rig": 80}
    r, _, _ = make(nm)
    check("highest autoconnect-priority wins over signal", r.tick() == "joined:android-hotspot")
    nm = FakeNM(); nm.add("Weak"); nm.add("Strong"); nm.visible = {"Weak": 20, "Strong": 80}
    r, _, _ = make(nm)
    check("same priority -> strongest signal", r.tick() == "joined:Strong")
    nm = FakeNM(); nm.add("Odd:Name", ssid="Odd:SSID"); nm.visible = {"Odd:SSID": 60}
    r, _, _ = make(nm)
    check("':' in names/SSIDs (nmcli escaping)", r.tick() == "joined:Odd:Name")

    print("failure backoff")
    nm = FakeNM(); nm.add("WrongPass"); nm.add("Backup"); nm.visible = {"WrongPass": 90, "Backup": 40}
    nm.up_rc = {"WrongPass": 4}
    r, clk, _ = make(nm)
    check("failed up reported", r.tick() == "failed:WrongPass")
    clk.t += 15
    check("next tick tries the other network", r.tick() == "joined:Backup")
    nm = FakeNM(); nm.add("WrongPass"); nm.visible = {"WrongPass": 90}; nm.up_rc = {"WrongPass": 4}
    r, clk, _ = make(nm)
    r.tick()
    clk.t += 60
    check("inside backoff -> not retried", r.tick() == "nothing" and nm.ups() == ["WrongPass"])
    clk.t += R.FAIL_BACKOFF_S
    check("after backoff -> retried", r.tick() == "failed:WrongPass" and nm.ups() == ["WrongPass", "WrongPass"])

    print("periodic re-tune + one watchdog per box")
    lock = os.path.join(tempfile.mkdtemp(), "wifi-rejoin.lock")
    nm = FakeNM(); nm.add("ChocolateSalty"); nm.state = "connected"
    r, clk, _ = make(nm, shared_lock=lock)
    r.tick()
    check("first tick tunes", nm.profiles["ChocolateSalty"]["connection.autoconnect-retries"] == "0")
    nm.profiles["ChocolateSalty"]["connection.autoconnect-retries"] = "-1 (default)"
    clk.t += 60
    r.tick()
    check("not re-tuned inside TUNE_EVERY_S", nm.profiles["ChocolateSalty"]["connection.autoconnect-retries"] == "-1 (default)")
    clk.t += R.TUNE_EVERY_S
    r.tick()
    check("re-tuned after TUNE_EVERY_S", nm.profiles["ChocolateSalty"]["connection.autoconnect-retries"] == "0")
    nm_b = FakeNM(); nm_b.add("A"); nm_b.visible = {"A": 50}
    rb, _, _ = make(nm_b, shared_lock=lock)
    check("second app on the same box stands down", rb.tick() == "not_owner" and nm_b.calls == [])
    os.close(r._lock_fd)
    r._lock_fd = None
    check("...and takes over once the first lets go", rb.tick() == "joined:A")

    print()
    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
