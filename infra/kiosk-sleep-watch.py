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
SLEEP_AFTER_HRS  = 3.0   # tweak this, then: sudo systemctl restart kiosk-sleep-watch.timer
LAST_ACTIVE_PATH = "/home/pi/.cache/lightboard_kiosk_last_active"
XSET_ENV         = {"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"}


def read_last_active():
    try:
        with open(LAST_ACTIVE_PATH) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def write_last_active(ts):
    os.makedirs(os.path.dirname(LAST_ACTIVE_PATH), exist_ok=True)
    with open(LAST_ACTIVE_PATH, "w") as f:
        f.write(str(ts))


def fetch_state():
    with urllib.request.urlopen(STATE_URL, timeout=5) as r:
        return json.load(r)


def sleep_screen():
    env = dict(os.environ)
    env.update(XSET_ENV)
    subprocess.run(["xset", "dpms", "force", "off"], env=env, check=False)


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

    last_active = read_last_active()
    if showing_light or last_active is None:
        # Light is on, or this is our first-ever run - (re)start the clock
        # rather than assuming idle time we can't actually account for.
        write_last_active(now)
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
