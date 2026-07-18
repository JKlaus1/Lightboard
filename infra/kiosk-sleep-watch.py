#!/usr/bin/env python3
"""
kiosk-sleep-watch.py — puts the kiosk touchscreen to sleep after a stretch
of no light output, so the panel isn't lit 24/7 between gigs. Wakes back up
on touch automatically: DPMS stays enabled (see the openbox autostart —
`xset +dpms; xset dpms 0 0 0`, no auto-timeout), so this script is the
*only* thing that ever puts the screen to sleep, and any input event (a
tap) brings it straight back per X's normal DPMS behavior.

"No light output" = no active scenes, OR blackout is fully engaged. Either
one starts the idle countdown — a blackout at closing counts the same as
Clear All, since both leave the stage dark.

Run every few minutes via kiosk-sleep-watch.timer (systemd). Safe to run
by hand too:  python3 kiosk-sleep-watch.py
"""
import json
import os
import subprocess
import time
import urllib.request

STATE_URL        = "http://localhost:5000/api/state"
SLEEP_AFTER_HRS  = 2.0   # tweak this, then: sudo systemctl restart kiosk-sleep-watch.timer
LAST_ACTIVE_PATH = "/home/pi/.cache/lightboard_kiosk_last_active"
XSET_ENV         = {"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"}


def read_last_active():
    try:
        with open(LAST_ACTIVE_PATH) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def get_boot_time():
    """System boot time (epoch seconds) via /proc/uptime (monotonic, so it's
    unaffected by wall-clock weirdness). Used to catch a last-active
    timestamp left over from BEFORE this boot - LAST_ACTIVE_PATH lives on
    the SD card and survives a reboot/power-cut, so without this check a
    fresh boot with no lights on yet could inherit hours of "idle" time
    that actually happened last session, and sleep the screen almost
    immediately instead of after a real SLEEP_AFTER_HRS of this boot's
    uptime."""
    try:
        with open("/proc/uptime") as f:
            uptime_seconds = float(f.read().split()[0])
        return time.time() - uptime_seconds
    except (FileNotFoundError, ValueError, IndexError):
        return None


def write_last_active(ts):
    os.makedirs(os.path.dirname(LAST_ACTIVE_PATH), exist_ok=True)
    with open(LAST_ACTIVE_PATH, "w") as f:
        f.write(str(ts))


def fetch_state():
    with urllib.request.urlopen(STATE_URL, timeout=5) as r:
        return json.load(r)


ASLEEP_SINCE_PATH = "/home/pi/.cache/lightboard_kiosk_asleep_since"


def sleep_screen():
    env = dict(os.environ)
    env.update(XSET_ENV)
    subprocess.run(["xset", "dpms", "force", "off"], env=env, check=False)
    # Marker for touch.html's wake-tap guard (batch item #2): the page polls
    # this via /api/kiosk/sleep-marker so it knows a DPMS-sleep happened even
    # though Chromium itself keeps running the whole time and has no other
    # way to observe display power state.
    try:
        os.makedirs(os.path.dirname(ASLEEP_SINCE_PATH), exist_ok=True)
        with open(ASLEEP_SINCE_PATH, "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        print(f"kiosk-sleep-watch: couldn't write asleep marker ({e})")


def main():
    now = time.time()
    try:
        state = fetch_state()
    except Exception as e:
        print(f"kiosk-sleep-watch: couldn't reach {STATE_URL} ({e}) - skipping this run")
        return

    active_scenes  = state.get("active_scenes") or []
    blackout_blend = state.get("blackout_blend", 0) or 0
    showing_light  = bool(active_scenes) and blackout_blend < 0.99

    last_active  = read_last_active()
    boot_time    = get_boot_time()
    stale_boot   = (last_active is not None and boot_time is not None
                     and last_active < boot_time)

    if showing_light or last_active is None or stale_boot:
        # Light is on, this is our first-ever run, or the saved timestamp
        # predates this boot - (re)start the clock rather than assuming
        # idle time we can't actually account for.
        write_last_active(now)
        if stale_boot:
            print("kiosk-sleep-watch: last-active predates this boot - idle clock reset")
        else:
            print("kiosk-sleep-watch: light is showing - idle clock reset")
        return

    idle_hours = (now - last_active) / 3600.0
    if idle_hours >= SLEEP_AFTER_HRS:
        print(f"kiosk-sleep-watch: idle {idle_hours:.1f}h >= {SLEEP_AFTER_HRS}h - sleeping screen")
        sleep_screen()
    else:
        print(f"kiosk-sleep-watch: idle {idle_hours:.1f}h / {SLEEP_AFTER_HRS}h - not yet")


if __name__ == "__main__":
    main()
