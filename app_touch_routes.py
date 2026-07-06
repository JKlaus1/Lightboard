# ── Touch screen UI ───────────────────────────────────────────────────────
#
# Add these routes to app.py just before the "# ── Run ───" block.
#
# Also add this import near the top of app.py (with the other imports):
#   import socket

@app.route("/touch")
def touch():
    return render_template("touch.html")

@app.route("/touch-config")
def touch_config():
    return render_template("touch_config.html")

@app.route("/api/touch/info")
def api_touch_info():
    """Returns Pi IP address and active show name for the touch screen footer."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "unknown"
    return jsonify({
        "ip":        ip,
        "show_name": show_config.get("name", "Lightboard"),
    })

@app.route("/api/touch/config", methods=["GET"])
def api_touch_config_get():
    """Return the current touch screen grid config."""
    cfg = config.get("touch_grid", {"cols": 2, "rows": 6, "cells": []})
    return jsonify(cfg)

def _clean_cell(c):
    """Light normalization for one touch-grid cell: clamp w/h to sane bounds
    (1-12, matching the fader-def convention) and leave everything else
    untouched. Empty slots (None) pass through as-is."""
    if not isinstance(c, dict):
        return c
    out = dict(c)
    for k in ("w", "h"):
        try:
            out[k] = max(1, min(12, int(c.get(k, 1))))
        except (TypeError, ValueError):
            out[k] = 1
    return out

@app.route("/api/touch/config", methods=["POST"])
def api_touch_config_set():
    """Save the touch screen grid config into config.json."""
    data = request.json or {}
    config["touch_grid"] = {
        "cols":  int(data.get("cols", 2)),
        "rows":  int(data.get("rows", 6)),
        "cells": [_clean_cell(c) for c in data.get("cells", [])],
    }
    save_json(CONFIG_PATH, config)
    log.info("Touch grid config saved.")
    return jsonify({"ok": True})
