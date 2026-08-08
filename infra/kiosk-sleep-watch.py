#!/usr/bin/env python3
"""
kiosk-sleep-watch.py — puts the kiosk touchscreen to sleep after a stretch
of no light output, so the panel isn't lit 24/7 between gigs. Wakes back up
on touch automatically: DPMS stays enabled (see the openbox autostart —
`xset s off; xset +dpms; xset dpms 0 0 0`, no auto-timeout), so this script
is the *only* thing that ever puts the screen to sleep, and any input event
(a tap) brings it straight back per X's normal DPMS behavior.

"Light output" = anything visible on the rig: a main scene, motion, look,
effect, overlay, or the singer override. Blackout fully engaged counts as
dark no matter what's playing — a blackout at closing counts the same as
Clear All, since both leave the stage dark.

Run every few minutes via kiosk-sleep-watch.timer (systemd). Safe to run
by hand too:  python3 kiosk-sleep-watch.py

── 2026-08-07 fix ────────────────────────────────────────────────────────
This script previously read state["active_scenes"], a key /api/state has
never returned (the main-scene list is "scenes"). It was therefore always
falsy, "showing light" was always False, the idle clock NEVER reset, and
once uptime passed SLEEP_AFTER_HRS every timer tick re-slept the screen —
blanking the panel a few minutes after each wake regardless of what was
playing. Fixed here, along with:
  * effects/motions/looks/overlay/singer now count as light (running a
    plasma effect with no main scene used to read as idle),
  * a manual wake (user taps a sleeping screen) restarts the idle clock
    instead of being re-slept on the next tick,
  * the asleep marker is only written on the sleep TRANSITION, so
    touch.html's wake-tap guard sees one timestamp per sleep rather than a
    fresh one every 5 minutes.
"""
import json
import os
import subprocess
import time
import urllib.request

STATE_URL         = "http://localhost:5000/api/state"
SLEEP_AFTER_HRS   = 2.0   # tweak this, then: sudo systemctl restart kiosk-sleep-watch.timer
LAST_ACTIVE_PATH  = "/home/pi/.cache/lightboard_kiosk_last_active"
ASLEEP_SINCE_PATH = "/home/pi/.cache/lightboard_kiosk_asleep_since"
XSET_ENV          = {"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"}

# Every /api/state key holding a stack of currently-playing layers. A
# non-empty list in ANY of these means something is rendering to the rig.
# Names verified against engine.get_state() — keep them in sync if the
# state payload is ever renamed.
LIGHT_STACKS = ("scenes", "motions", "looks", "effects")

# Blackout blend at/above this is treated as fully dark.
BLACKOUT_DARK = 0.99


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


def showing_light(state):
    """True if anything is visibly lit on the rig right now.

    Deliberately does NOT factor in the master/singer dimmer levels. A scene
    playing at 5% master is still 'in use' from the operator's point of view,
    and biasing toward 'lit' means the worst case is a screen that stays on
    too long rather than one that blanks mid-show."""
    if float(state.get("blackout_blend") or 0) >= BLACKOUT_DARK:
        return False        # blackout swallows everything below it
    if any(state.get(k) for k in LIGHT_STACKS):
        return True
    if state.get("overlay_active") or float(state.get("overlay_blend") or 0) > 0.01:
        return True
    # Singer pods can be lit with no scene behind them.
    if state.get("singer_mode") and float(state.get("singer_level") or 0) > 0:
        return True
    return False


def monitor_is_off():
    """True/False from X's DPMS state, or None if X can't be reached."""
    env = dict(os.environ)
    env.update(XSET_ENV)
    try:
        out = subprocess.run(["xset", "q"], env=env, capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "Monitor is Off" in out:
        return True
    if "Monitor is On" in out:
        return False
    return None


def we_slept_it():
    return os.path.exists(ASLEEP_SINCE_PATH)


def clear_asleep_marker():
    try:
        os.remove(ASLEEP_SINCE_PATH)
    except OSError:
        pass


def sleep_screen():
    env = dict(os.environ)
    env.update(XSET_ENV)
    subprocess.run(["xset", "dpms", "force", "off"], env=env, check=False)
    # Marker for touch.html's wake-tap guard: the page polls this via
    # /api/kiosk/sleep-marker so it knows a DPMS-sleep happened even though
    # Chromium keeps running the whole time and has no other way to observe
    # display power state. Written once per sleep, on the transition only.
    try:
        os.makedirs(os.path.dirname(ASLEEP_SINCE_PATH), exist_ok=True)
        with open(ASLEEP_SINCE_PATH, "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        print(f"kiosk-sleep-watch: couldn't write asleep marker ({e})")


def main():
    now = time.time()

    # A screen we slept that is now on again means somebody tapped it. Treat
    # that as activity and restart the whole clock, otherwise the next tick
    # would blank it again within the timer interval.
    if we_slept_it() and monitor_is_off() is False:
        clear_asleep_marker()
        write_last_active(now)
        print("kiosk-sleep-watch: screen woken by touch - idle clock reset")
        return

    try:
        state = fetch_state()
    except Exception as e:
        print(f"kiosk-sleep-watch: couldn't reach {STATE_URL} ({e}) - skipping this run")
        return

    lit          = showing_light(state)
    last_active  = read_last_active()
    boot_time    = get_boot_time()
    stale_boot   = (last_active is not None and boot_time is not None
                     and last_active < boot_time)

    if lit or last_active is None or stale_boot:
        # Light is on, this is our first-ever run, or the saved timestamp
        # predates this boot - (re)start the clock rather than assuming
        # idle time we can't actually account for.
        write_last_active(now)
        if stale_boot:
            print("kiosk-sleep-watch: last-active predates this boot - idle clock reset")
        elif lit:
            print("kiosk-sleep-watch: light is showing - idle clock reset")
        else:
            print("kiosk-sleep-watch: first run - idle clock started")
        return

    idle_hours = (now - last_active) / 3600.0
    if idle_hours < SLEEP_AFTER_HRS:
        print(f"kiosk-sleep-watch: idle {idle_hours:.1f}h / {SLEEP_AFTER_HRS}h - not yet")
        return

    if we_slept_it() or monitor_is_off() is True:
        # Already asleep - don't re-issue the force-off (and don't rewrite the
        # marker, which would keep resetting touch.html's wake guard).
        print(f"kiosk-sleep-watch: idle {idle_hours:.1f}h - already asleep")
        return

    print(f"kiosk-sleep-watch: idle {idle_hours:.1f}h >= {SLEEP_AFTER_HRS}h - sleeping screen")
    sleep_screen()


if __name__ == "__main__":
    main()
