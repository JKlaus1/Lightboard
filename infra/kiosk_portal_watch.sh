#!/bin/bash
# kiosk_portal_watch.sh — runs in the Pi's graphical (kiosk) session via autostart.
# Watches a command file written by lightboard's WiFi page and switches the kiosk
# Chromium between the normal touch UI and a captive-portal page.
#   command file:  a URL   -> show that URL fullscreen (portal login), in a throwaway profile
#                  "restore" -> relaunch the normal touch kiosk
# Lightboard only writes the file; this script (already inside the GUI session) does
# the browser control, so there's no fragile cross-session GUI access.

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

CMD_FILE="/tmp/lightboard_kiosk_cmd"
TOUCH_URL="http://localhost:5000/touch"
PORTAL_PROFILE="/tmp/portal-chromium"
CHROME="$(command -v chromium || command -v chromium-browser)"

# Flags mirrored from ~/kiosk.sh so both look/behave the same on the touchscreen.
FLAGS=(--kiosk --noerrdialogs --disable-infobars --no-first-run
       --disable-features=TranslateUI --disable-session-crashed-bubble
       --disable-restore-session-state --touch-events=enabled --disable-pinch
       --force-device-scale-factor=1)

last=""
while true; do
  if [ -f "$CMD_FILE" ]; then
    cmd="$(cat "$CMD_FILE" 2>/dev/null)"
    if [ "$cmd" != "$last" ]; then
      last="$cmd"
      pkill -f chromium 2>/dev/null
      sleep 1
      if [ "$cmd" = "restore" ]; then
        # Relaunch the touch kiosk directly (skips kiosk.sh's boot-time sleep 8).
        ( "$CHROME" "${FLAGS[@]}" "$TOUCH_URL" >/dev/null 2>&1 & )
      elif [ -n "$cmd" ]; then
        # Fresh throwaway profile -> nothing to restore, no lock fight with the kiosk profile.
        rm -rf "$PORTAL_PROFILE" 2>/dev/null
        ( "$CHROME" "${FLAGS[@]}" --user-data-dir="$PORTAL_PROFILE" "$cmd" >/dev/null 2>&1 & )
      fi
    fi
  fi
  sleep 1
done
