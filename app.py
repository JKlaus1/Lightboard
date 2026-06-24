"""
Lightboard Pi — Flask web server.
Serves the control UI and REST API.
"""

import json
import io
import os
import zipfile
import logging
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file

from dmx import EnttecOpenDMX
from artnet import ArtNetDMX
from sacn import SacnDMX
from engine import LightingEngine
import cell_strip
import effects
from wifi_routes import register_wifi_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)

# ── Bootstrap ─────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent
CONFIG_PATH  = BASE_DIR / "config.json"
PALETTE_PATH = BASE_DIR / "palette.json"

def load_json(path):
    with open(path) as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

config     = load_json(CONFIG_PATH)
SHOWS_DIR  = Path(config["shows_dir"])

# ── Color palette ─────────────────────────────────────────────────────────
# A single shared palette feeds every color picker. Each color carries one
# recipe per "engine" (RGB, RGBAWUV, RGBLAUV, …); the picker resolves the
# recipe for whatever engine a fixture exposes. Scenes still store raw channel
# dicts, so editing the palette never touches saved scenes.

DEFAULT_PALETTE = {
    "version": 1,
    "engines": {
        "rgb":     {"label": "RGB",     "channels": ["r", "g", "b"]},
        "rgbw":    {"label": "RGBW",    "channels": ["r", "g", "b", "w"]},
        "rgbaw":   {"label": "RGBAW",   "channels": ["r", "g", "b", "a", "w"]},
        "rgbawuv": {"label": "RGBAWUV", "channels": ["r", "g", "b", "a", "w", "uv"]},
        "rgblauv": {"label": "RGBLAUV", "channels": ["r", "g", "b", "l", "a", "uv"]},
    },
    "colors": [
        {"id": "off",   "name": "Off",   "recipes": {"rgb": {"r": 0, "g": 0, "b": 0}}},
        {"id": "white", "name": "White", "recipes": {"rgb": {"r": 255, "g": 255, "b": 255}}},
        {"id": "red",   "name": "Red",   "recipes": {"rgb": {"r": 255, "g": 0, "b": 0}}},
        {"id": "green", "name": "Green", "recipes": {"rgb": {"r": 0, "g": 255, "b": 0}}},
        {"id": "blue",  "name": "Blue",  "recipes": {"rgb": {"r": 0, "g": 0, "b": 255}}},
    ],
}

def load_palette():
    """Load the shared color palette, seeding a default file if missing."""
    if not PALETTE_PATH.exists():
        save_json(PALETTE_PATH, DEFAULT_PALETTE)
        log.info("Seeded default palette at %s", PALETTE_PATH)
        return DEFAULT_PALETTE
    try:
        return load_json(PALETTE_PATH)
    except Exception as e:
        log.error("Failed to load palette (%s); using built-in default", e)
        return DEFAULT_PALETTE

palette = load_palette()

def get_scenes_dir(show_id):
    return SHOWS_DIR / show_id / "scenes"

def load_show(show_id):
    return load_json(SHOWS_DIR / show_id / "show.json")

def load_scene(scenes_dir, scene_id):
    path = Path(scenes_dir) / f"{scene_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Scene not found: {scene_id}")
    return load_json(path)

def list_scenes(scenes_dir):
    """Legacy: list per-show scenes (used during migration only)."""
    scenes = []
    for f in sorted(Path(scenes_dir).glob("*.json")):
        try:
            data = load_json(f)
            scenes.append({
                "id":    f.stem,
                "name":  data.get("name", f.stem),
                "steps": len(data.get("steps", [])),
            })
        except Exception:
            pass
    return scenes

def sanitize_id(name):
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in name.lower().replace(" ", "_"))

# ── Scene Library ─────────────────────────────────────────────────────────

LIBRARY_DIR = BASE_DIR / "scene_library"
LIBRARY_DIR.mkdir(exist_ok=True)

import secrets

def gen_scene_id():
    """Generate a short URL-safe unique scene ID."""
    return secrets.token_urlsafe(6).replace('_','').replace('-','')[:8] or 'scn'

def library_path(scene_id):
    return LIBRARY_DIR / f"{scene_id}.json"

def load_library_scene(scene_id):
    path = library_path(scene_id)
    if not path.exists():
        raise FileNotFoundError(scene_id)
    return load_json(path)

def save_library_scene(scene_id, data):
    save_json(library_path(scene_id), data)

def list_library_scenes():
    """All scenes in library, with metadata."""
    scenes = []
    for f in sorted(LIBRARY_DIR.glob("*.json")):
        try:
            data = load_json(f)
            entry = {
                "id":         f.stem,
                "name":       data.get("name", f.stem),
                "folder":     data.get("folder", ""),
                "steps":      len(data.get("steps", [])),
                "scene_type": data.get("scene_type", "main"),
            }
            # Effect scenes have no steps — surface the effect id so the
            # library list can show "🌀 Comet" instead of "0 steps".
            if entry["scene_type"] == "effect":
                entry["effect"] = data.get("effect", "")
            scenes.append(entry)
        except Exception:
            pass
    return scenes

def get_show_scenes(show_cfg, scene_type=None):
    """Return scene metadata for the scenes enabled in this show, in order.
    If scene_type is provided, only scenes matching that type are returned."""
    enabled = show_cfg.get("enabled_scenes", [])
    scenes  = []
    for sid in enabled:
        try:
            data = load_library_scene(sid)
            st = data.get("scene_type", "main")
            if scene_type is not None and st != scene_type:
                continue
            entry = {
                "id":         sid,
                "name":       data.get("name", sid),
                "folder":     data.get("folder", ""),
                "steps":      len(data.get("steps", [])),
                "scene_type": st,
            }
            if st == "effect":
                entry["effect"] = data.get("effect", "")
            scenes.append(entry)
        except FileNotFoundError:
            pass
    return scenes

def show_has_movers(show_cfg):
    return any(fx.get("type") == "mover" for fx in show_cfg.get("fixtures", []))

def get_show_cycler_scenes(show_cfg):
    """Resolve the show's Beat Cycler scene list to {id,name,...} metadata.
    A cycler scene must be an enabled main scene. Falls back to every enabled
    main scene when the show hasn't configured a subset yet."""
    main = get_show_scenes(show_cfg, scene_type="main")
    ids  = show_cfg.get("cycler_scenes")
    if not ids:
        return main                       # default: all main scenes
    by_id = {m["id"]: m for m in main}
    return [by_id[sid] for sid in ids if sid in by_id]

def migrate_legacy_scenes():
    """One-time migration: copy per-show scenes into the global library
    and populate each show's `enabled_scenes` list."""
    # Skip if any library scenes already exist
    if any(LIBRARY_DIR.glob("*.json")):
        return

    log.info("Migrating per-show scenes to global library…")
    for show_dir in SHOWS_DIR.iterdir():
        if not show_dir.is_dir():
            continue
        legacy_scenes_dir = show_dir / "scenes"
        show_file         = show_dir / "show.json"
        if not (legacy_scenes_dir.exists() and show_file.exists()):
            continue
        show = load_json(show_file)
        if "enabled_scenes" in show:
            continue  # already migrated

        enabled = []
        folder_name = show.get("name", show_dir.name)
        for scene_file in sorted(legacy_scenes_dir.glob("*.json")):
            try:
                data = load_json(scene_file)
            except Exception:
                continue
            new_id = gen_scene_id()
            # Avoid collisions (extremely unlikely but safe)
            while library_path(new_id).exists():
                new_id = gen_scene_id()
            data["folder"] = data.get("folder", folder_name)
            data["name"]   = data.get("name", scene_file.stem)
            save_library_scene(new_id, data)
            enabled.append(new_id)
            log.info(f"  migrated: {scene_file.name} → {new_id} (folder: {folder_name})")

        show["enabled_scenes"] = enabled
        save_json(show_file, show)
    log.info("Scene library migration complete.")

# ── Global state ──────────────────────────────────────────────────────────

show_config = load_show(config["active_show"])
scenes_dir  = get_scenes_dir(config["active_show"])  # kept for legacy import compat

# One-time migration of per-show scenes into the global library
migrate_legacy_scenes()
# Reload show config in case migration added enabled_scenes
show_config = load_show(config["active_show"])

def _create_dmx_driver(cfg):
    """Build a DMX output driver based on config:
      dmx_driver = 'enttec' : EnttecOpenDMX over USB-serial (default)
      dmx_driver = 'artnet' : ArtNetDMX over UDP to one or more Art-Net nodes
      dmx_driver = 'sacn'   : SacnDMX over UDP (E1.31), unicast or multicast
    """
    driver = cfg.get("dmx_driver", "enttec").lower()
    if driver == "artnet":
        return ArtNetDMX(
            targets=cfg.get("artnet_target", ""),
            universe=cfg.get("artnet_universe", 0),
        )
    if driver == "sacn":
        return SacnDMX(
            targets=cfg.get("sacn_target", ""),
            universe=cfg.get("sacn_universe", 1),
            priority=cfg.get("sacn_priority", 100),
            multicast=cfg.get("sacn_multicast", True),
        )
    return EnttecOpenDMX(cfg.get("dmx_port", "/dev/ttyUSB0"))

dmx = _create_dmx_driver(config)
dmx.connect()

engine = LightingEngine(dmx, show_config)

startup = show_config.get("startup_scene")
if startup:
    try:
        scene = load_library_scene(startup)
        engine.play_scene(scene, launch_fade_ms=2000, scene_id=startup)
        log.info(f"Startup scene '{startup}' loaded")
    except Exception as e:
        log.warning(f"Startup scene failed: {e}")

# ── App ───────────────────────────────────────────────────────────────────

app = Flask(__name__)
register_wifi_routes(app)  # venue WiFi page (/wifi)
app.config["TEMPLATES_AUTO_RELOAD"] = True

@app.after_request
def add_no_cache_headers(response):
    """Prevent browser caching of HTML pages so updates take effect immediately."""
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma']        = 'no-cache'
        response.headers['Expires']       = '0'
    return response

# ── Pages ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html",
        show=show_config,
        scenes=get_show_scenes(show_config, scene_type="main"),
        cycler_scenes=get_show_cycler_scenes(show_config),
        library_scenes=list_library_scenes(),
        motion_scenes=get_show_scenes(show_config, scene_type="mover_motion"),
        look_scenes=get_show_scenes(show_config, scene_type="mover_look"),
        effect_scenes=get_show_scenes(show_config, scene_type="effect"),
        has_movers=show_has_movers(show_config),
        shows=_list_shows(),
        active_show=config["active_show"],
    )

@app.route("/editor")
@app.route("/editor/<scene_id>")
def editor(scene_id=None):
    scene = None
    if scene_id:
        try:
            scene = load_library_scene(scene_id)
            log.info(f"Editor loaded scene '{scene_id}': {len(scene.get('steps',[]))} steps")
        except Exception as e:
            log.warning(f"Editor: failed to load scene '{scene_id}': {e}")
    # Determine scene type — from existing scene, or from ?type=... query param, default 'main'
    if scene is not None:
        scene_type = scene.get("scene_type", "main")
    else:
        scene_type = request.args.get("type", "main")
        if scene_type not in ("main", "mover_motion", "mover_look", "effect"):
            scene_type = "main"
    folders = []
    for f in LIBRARY_DIR.glob("*.json"):
        try:
            d = load_json(f)
            fld = d.get("folder", "").strip()
            if fld and fld not in folders:
                folders.append(fld)
        except Exception:
            pass
    # Effect scenes get their own editor (Phase 2D) — completely different
    # data model (no steps, no per-pod participation), so a separate template
    # keeps both files manageable.
    template = "effect_editor.html" if scene_type == "effect" else "editor.html"
    return render_template(template,
        show=show_config,
        scene=scene,
        scene_id=scene_id,
        scene_type=scene_type,
        folders=sorted(folders),
        palette=palette,
    )

@app.route("/library")
def library_page():
    return render_template("library.html",
        show=show_config,
        active_show=config["active_show"],
        shows=_list_shows(),
    )


# Add these routes to app.py just before the "
@app.route("/api/touch/reload", methods=["POST"])
def api_touch_reload():
    """Signal the kiosk to reload by writing a timestamp."""
    import time
    config["touch_reload_ts"] = time.time()
    return jsonify({"ok": True})

@app.route("/api/touch/reload", methods=["GET"])
def api_touch_reload_check():
    """Kiosk polls this to know if it should reload."""
    return jsonify({"ts": config.get("touch_reload_ts", 0)})

# ── Run ───" block.
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
    import subprocess
    ips = {}
    try:
        for iface in ["wlan0", "eth0"]:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", iface],
                capture_output=True, text=True
            )
            dynamic_ip = None
            first_ip   = None
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line.startswith("inet "):
                    continue
                tokens = line.split()
                addr   = tokens[1].split("/")[0]
                if first_ip is None:
                    first_ip = addr
                if "dynamic" in tokens:   # DHCP lease — prefer this
                    dynamic_ip = addr
                    break
            chosen = dynamic_ip or first_ip
            if chosen:
                ips[iface] = chosen
    except Exception:
        pass
    ip = ips.get("wlan0") or ips.get("eth0") or "unknown"
    return jsonify({
        "ip":        ip,
        "show_name": show_config.get("name", "Lightboard"),
        "ips":       ips,
    })

@app.route("/api/touch/config", methods=["GET"])
def api_touch_config_get():
    """Return the current touch screen grid config."""
    cfg = config.get("touch_grid", {"cols": 2, "rows": 6, "cells": []})
    return jsonify(cfg)

@app.route("/api/touch/config", methods=["POST"])
def api_touch_config_set():
    """Save the touch screen grid config into config.json."""
    data = request.json or {}
    config["touch_grid"] = {
        "cols":  int(data.get("cols", 2)),
        "rows":  int(data.get("rows", 6)),
        "cells": data.get("cells", []),
    }
    save_json(CONFIG_PATH, config)
    log.info("Touch grid config saved.")
    return jsonify({"ok": True})


@app.route("/messenger")
def messenger_page():
    """Stage Messenger control panel embedded in Lightboard."""
    messenger_host = config.get("messenger_host", "")
    messenger_port = config.get("messenger_port", 3000)
    return render_template("messenger.html",
        show=show_config,
        messenger_host=messenger_host,
        messenger_port=messenger_port,
    )

@app.route("/api/messenger-config")
def api_messenger_config():
    """Return Stage Messenger connection settings for the popup listener."""
    return jsonify({
        "host": config.get("messenger_host", ""),
        "port": config.get("messenger_port", 3000),
    })

# ── State ─────────────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    state = engine.get_state()
    # The engine returns "scenes" = currently active main scenes (the new
    # multi-scene playback list). The full list of scenes enabled in the
    # current show is exposed separately under "enabled_scenes" so the two
    # don't collide. Library/editor pages read enabled_scenes to know which
    # scenes are in the show.
    state["enabled_scenes"] = get_show_scenes(show_config)
    return jsonify(state)

@app.route("/api/dmx")
def api_dmx():
    """Return current DMX data as {universe_num: [512 channel values]}.
    Each list is 0-indexed where index 0 = channel 1."""
    return jsonify(dmx.get_all_universes_snapshot())

# ── Playback ──────────────────────────────────────────────────────────────

@app.route("/api/scene/<scene_id>", methods=["POST"])
def api_play(scene_id):
    try:
        scene = load_library_scene(scene_id)
        fade  = (request.json or {}).get("launch_fade")
        engine.play_scene(scene, launch_fade_ms=fade, scene_id=scene_id)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Scene not found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Stop scene(s). Body can include {scene_id: "..."} to stop one specific
    scene. Without scene_id, all main scenes are stopped (with fade)."""
    data = request.json or {}
    scene_id = data.get("scene_id")
    engine.stop_scene(scene_id=scene_id)
    return jsonify({"ok": True})

@app.route("/api/stop/<scene_id>", methods=["POST"])
def api_stop_one(scene_id):
    """Stop a specific scene by ID (with fade)."""
    engine.stop_scene(scene_id=scene_id)
    return jsonify({"ok": True})

@app.route("/api/stop-all", methods=["POST"])
def api_stop_all():
    """Hard-stop all main scenes immediately (no fade)."""
    engine.stop_all_scenes()
    return jsonify({"ok": True})

@app.route("/api/freeze", methods=["POST"])
def api_freeze():
    """Toggle freeze on/off. Body: {enable: bool}."""
    data = request.json or {}
    enable = bool(data.get("enable", False))
    engine.set_freeze(enable)
    return jsonify({"ok": True, "freeze": engine.get_freeze_state()})

# ── Tempo / tap-tempo ──────────────────────────────────────────────────────

@app.route("/api/tempo/tap", methods=["POST"])
def api_tempo_tap():
    """Register one tap. The new BPM only commits once tapping settles, so
    repeated calls during a burst don't retime the live show. The returned
    snapshot includes preview_bpm (live estimate) for button feedback."""
    return jsonify({"ok": True, "tempo": engine.tap()})

@app.route("/api/tempo/cancel", methods=["POST"])
def api_tempo_cancel():
    """Zero out tap tempo — synced scenes/effects revert to their own defaults
    and the cycler stops."""
    return jsonify({"ok": True, "tempo": engine.tempo_cancel()})

@app.route("/api/tempo/nudge", methods=["POST"])
def api_tempo_nudge():
    """Trim the committed BPM, preserving phase. Body: {delta: <float>}."""
    delta = float((request.json or {}).get("delta", 0))
    return jsonify({"ok": True, "tempo": engine.tempo_nudge(delta)})

@app.route("/api/tempo/resync", methods=["POST"])
def api_tempo_resync():
    """Drop a downbeat now without changing BPM."""
    return jsonify({"ok": True, "tempo": engine.tempo_resync()})

@app.route("/api/tempo", methods=["GET"])
def api_tempo_get():
    """Current tempo snapshot (also embedded in /api/state under 'tempo')."""
    return jsonify(engine.tempo_status())

# ── Beat cycler ─────────────────────────────────────────────────────────────

@app.route("/api/cycler/start", methods=["POST"])
def api_cycler_start():
    """Arm the beat cycler. Body:
        {scene_ids: ["id1","id2",...], division: <beats per look>,
         crossfade_ms: <optional int>}
    Loads each scene from the library and hands the dicts to the engine. If no
    tempo is live yet the cycler waits (armed) until one is tapped in."""
    data      = request.json or {}
    scene_ids = data.get("scene_ids") or []
    if not scene_ids:
        return jsonify({"ok": False, "error": "No scene_ids provided"}), 400
    scenes, missing = [], []
    for sid in scene_ids:
        try:
            scenes.append(load_library_scene(sid))
        except FileNotFoundError:
            missing.append(sid)
    if not scenes:
        return jsonify({"ok": False, "error": "No valid scenes",
                        "missing": missing}), 404
    status = engine.start_cycler(
        scenes,
        division=float(data.get("division", 1.0)),
        crossfade_ms=data.get("crossfade_ms"),
    )
    return jsonify({"ok": True, "cycler": status, "missing": missing})

@app.route("/api/cycler/stop", methods=["POST"])
def api_cycler_stop():
    """Stop the cycler and fade out its decks."""
    return jsonify({"ok": True, "cycler": engine.stop_cycler()})

@app.route("/api/cycler", methods=["GET"])
def api_cycler_get():
    """Current cycler snapshot (also embedded in /api/state under 'cycler')."""
    return jsonify(engine.cycler_status())

@app.route("/api/cycler/toggle", methods=["POST"])
def api_cycler_toggle():
    """Start the cycler from the active show's configured cycler scenes, or stop
    it if already running. Body (optional): {division, crossfade_ms}. Used by the
    touchscreen, which has no multi-select UI."""
    data = request.json or {}
    if engine.cycler_status().get("active"):
        return jsonify({"ok": True, "cycler": engine.stop_cycler(), "running": False})
    scenes, missing = [], []
    for m in get_show_cycler_scenes(show_config):
        try:
            scenes.append(load_library_scene(m["id"]))
        except FileNotFoundError:
            missing.append(m["id"])
    if not scenes:
        return jsonify({"ok": False, "error": "No cycler scenes configured for this show"}), 400
    status = engine.start_cycler(
        scenes,
        division=float(data.get("division", 1.0)),
        crossfade_ms=data.get("crossfade_ms"),
    )
    return jsonify({"ok": True, "cycler": status, "running": True, "missing": missing})

# ── Mover layer playback ──────────────────────────────────────────────────

@app.route("/api/motion/<scene_id>", methods=["POST"])
def api_play_motion(scene_id):
    try:
        scene = load_library_scene(scene_id)
        engine.play_motion_scene(scene, scene_id=scene_id)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Scene not found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/motion/stop", methods=["POST"])
def api_stop_motion():
    engine.stop_motion_scene()
    return jsonify({"ok": True})

@app.route("/api/look/<scene_id>", methods=["POST"])
def api_play_look(scene_id):
    try:
        scene = load_library_scene(scene_id)
        engine.play_look_scene(scene, scene_id=scene_id)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Scene not found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/look/stop", methods=["POST"])
def api_stop_look():
    engine.stop_look_scene()
    return jsonify({"ok": True})

# ── Live preview from editor ──────────────────────────────────────────────

@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Push a live preview frame to the engine for the given scene type."""
    data = request.json or {}
    scene_type = data.get("scene_type", "main")
    if scene_type not in ("main", "mover_motion", "mover_look"):
        return jsonify({"ok": False, "error": "Invalid scene_type"}), 400
    fixtures = data.get("fixtures") or {}
    engine.preview_set(scene_type, fixtures)
    return jsonify({"ok": True})

@app.route("/api/preview/stop", methods=["POST"])
def api_preview_stop():
    scene_type = (request.json or {}).get("scene_type", "main")
    engine.preview_clear(scene_type)
    return jsonify({"ok": True})

@app.route("/api/dmx/raw", methods=["POST"])
def api_dmx_raw():
    """Raw channel test override for the fixture builder's discovery panel.
    Body: {"channels": [[universe, ch, value], ...], "solo": bool}.
    Replaces the entire override set each call; POST /api/dmx/raw/clear to
    release."""
    data  = request.json or {}
    chans = data.get("channels") or []
    solo  = data.get("solo")
    try:
        triples = [(int(u), int(c), int(v)) for u, c, v in chans]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Bad channel data"}), 400
    engine.raw_set(triples, solo=solo)
    return jsonify({"ok": True})

@app.route("/api/dmx/raw/clear", methods=["POST"])
def api_dmx_raw_clear():
    engine.raw_clear()
    return jsonify({"ok": True})

@app.route("/api/restart", methods=["POST"])
def api_restart():
    """Restart the lightboard systemd service.

    The HTTP response is fired before the restart happens — the client never
    actually sees a response body, since the process exits right after. The
    UI polls /api/state to detect when the new process is back up.
    """
    import threading, subprocess, time
    def _do_restart():
        # Brief delay so the HTTP response gets flushed to the client first
        time.sleep(0.3)
        try:
            # systemctl restart kills this process and starts a fresh one,
            # so this call won't return.
            subprocess.run(
                ["sudo", "systemctl", "restart", "lightboard.service"],
                check=False,
            )
        except Exception as e:
            log.error(f"Restart failed: {e}")
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True, "msg": "restarting"})

@app.route("/api/dmx/config", methods=["GET"])
def api_dmx_config_get():
    return jsonify({
        "driver":          config.get("dmx_driver", "enttec"),
        "dmx_port":        config.get("dmx_port", "/dev/ttyUSB0"),
        "artnet_target":   config.get("artnet_target", ""),
        "artnet_universe": config.get("artnet_universe", 0),
        "sacn_target":     config.get("sacn_target", ""),
        "sacn_universe":   config.get("sacn_universe", 1),
        "sacn_priority":   config.get("sacn_priority", 100),
        "sacn_multicast":  config.get("sacn_multicast", True),
        "connected":       dmx.connected,
    })

@app.route("/api/dmx/config", methods=["POST"])
def api_dmx_config_set():
    """Save DMX output config AND hot-swap the running driver."""
    global dmx
    data = request.json or {}
    driver = data.get("driver", "enttec").lower()
    if driver not in ("enttec", "artnet", "sacn"):
        return jsonify({"ok": False, "error": "Driver must be 'enttec', 'artnet', or 'sacn'"}), 400

    config["dmx_driver"] = driver
    if "dmx_port" in data:
        config["dmx_port"] = data["dmx_port"]
    if "artnet_target" in data:
        config["artnet_target"] = data["artnet_target"]
    if "artnet_universe" in data:
        try:
            config["artnet_universe"] = int(data["artnet_universe"])
        except (ValueError, TypeError):
            config["artnet_universe"] = 0
    if "sacn_target" in data:
        config["sacn_target"] = data["sacn_target"]
    if "sacn_universe" in data:
        try:
            config["sacn_universe"] = int(data["sacn_universe"])
        except (ValueError, TypeError):
            config["sacn_universe"] = 1
    if "sacn_priority" in data:
        try:
            config["sacn_priority"] = max(0, min(200, int(data["sacn_priority"])))
        except (ValueError, TypeError):
            config["sacn_priority"] = 100
    if "sacn_multicast" in data:
        config["sacn_multicast"] = bool(data["sacn_multicast"])
    save_json(CONFIG_PATH, config)

    # Hot-swap the running driver
    try:
        new_driver = _create_dmx_driver(config)
        new_driver.connect()
        old_driver = engine.set_dmx(new_driver)
        dmx = new_driver
        # Disconnect old AFTER swap so the output loop never touches a dead driver
        try:
            old_driver.disconnect()
        except Exception as e:
            log.warning(f"Old DMX driver disconnect: {e}")
        log.info(f"DMX driver swapped to: {driver}")
        return jsonify({"ok": True, "driver": driver, "connected": new_driver.connected})
    except Exception as e:
        log.error(f"DMX swap failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/blackout", methods=["POST"])
def api_blackout():
    mode = (request.json or {}).get("mode", "full")
    engine.blackout(mode=mode)
    return jsonify({"ok": True, "mode": mode})

@app.route("/api/overlay", methods=["POST"])
def api_overlay():
    """Activate, deactivate, or toggle the overlay scene configured for the active show.
    JSON body: {"action": "toggle" | "on" | "off"}  (default: toggle)."""
    overlay_id = show_config.get("overlay_scene")
    if not overlay_id:
        return jsonify({"ok": False, "error": "No overlay scene assigned for this show"}), 400
    try:
        scene = load_library_scene(overlay_id)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Overlay scene not found in library"}), 404
    action = (request.json or {}).get("action", "toggle")
    if action == "on":
        engine.start_overlay(scene)
    elif action == "off":
        engine.stop_overlay()
    else:
        engine.toggle_overlay(scene)
    return jsonify({"ok": True, "action": action})

# ── Controls ──────────────────────────────────────────────────────────────

@app.route("/api/master", methods=["POST"])
def api_master():
    level = float((request.json or {}).get("level", 1.0))
    engine.set_master(level)
    return jsonify({"ok": True, "level": level})

@app.route("/api/singer/mode", methods=["POST"])
def api_singer_mode():
    enabled = bool((request.json or {}).get("enabled", True))
    engine.set_singer_mode(enabled)
    return jsonify({"ok": True, "enabled": enabled})

@app.route("/api/singer/level", methods=["POST"])
def api_singer_level():
    level = float((request.json or {}).get("level", 1.0))
    engine.set_singer_level(level)
    return jsonify({"ok": True, "level": level})

# ── Scene management (library-based) ──────────────────────────────────────

@app.route("/api/scenes")
def api_scenes():
    """Return scenes enabled for the active show."""
    return jsonify(get_show_scenes(show_config))

@app.route("/api/library/scenes")
def api_library_list():
    """Return ALL scenes in the global library."""
    return jsonify(list_library_scenes())

@app.route("/api/library/folders")
def api_library_folders():
    """List unique folder paths used across the library."""
    folders = set()
    for f in LIBRARY_DIR.glob("*.json"):
        try:
            data = load_json(f)
            folder = data.get("folder", "").strip()
            if folder:
                folders.add(folder)
                # Also add parent folders for nesting
                parts = folder.split("/")
                for i in range(1, len(parts)):
                    folders.add("/".join(parts[:i]))
        except Exception:
            pass
    return jsonify(sorted(folders))

@app.route("/api/library/scene/<scene_id>", methods=["GET"])
def api_library_get(scene_id):
    try:
        data = load_library_scene(scene_id)
        data["id"] = scene_id
        return jsonify(data)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Not found"}), 404

@app.route("/api/library/scene/<scene_id>", methods=["POST"])
def api_library_save(scene_id):
    """Update an existing scene."""
    data = request.json
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400
    if not library_path(scene_id).exists():
        return jsonify({"ok": False, "error": "Scene not found"}), 404
    save_library_scene(scene_id, data)
    # If this scene is currently playing, hot-swap the new look in live so an
    # edit-and-save updates immediately (no need to toggle it off/on).
    try:
        if (data.get("scene_type") or "main") == "effect":
            engine.refresh_active_effect(scene_id, data)
        else:
            engine.refresh_active_scene(scene_id, data)
    except Exception:
        pass
    return jsonify({"ok": True, "id": scene_id})

@app.route("/api/library/scene", methods=["POST"])
def api_library_new():
    """Create a new scene. Returns the assigned ID."""
    data = request.json
    if not data or "name" not in data:
        return jsonify({"ok": False, "error": "Missing name"}), 400
    new_id = gen_scene_id()
    while library_path(new_id).exists():
        new_id = gen_scene_id()
    save_library_scene(new_id, data)
    return jsonify({"ok": True, "id": new_id})

@app.route("/api/library/scene/<scene_id>", methods=["DELETE"])
def api_library_delete(scene_id):
    p = library_path(scene_id)
    if not p.exists():
        return jsonify({"ok": False, "error": "Not found"}), 404
    p.unlink()
    # Also remove from any show's enabled_scenes
    global show_config
    for show_dir in SHOWS_DIR.iterdir():
        sf = show_dir / "show.json"
        if not sf.exists():
            continue
        try:
            s = load_json(sf)
            if scene_id in s.get("enabled_scenes", []):
                s["enabled_scenes"] = [x for x in s["enabled_scenes"] if x != scene_id]
                save_json(sf, s)
                if show_dir.name == config["active_show"]:
                    show_config = s
        except Exception:
            pass
    return jsonify({"ok": True})

@app.route("/api/library/scene/<scene_id>/duplicate", methods=["POST"])
def api_library_duplicate(scene_id):
    try:
        data = load_library_scene(scene_id)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Not found"}), 404
    data["name"] = data.get("name", scene_id) + " (copy)"
    new_id = gen_scene_id()
    while library_path(new_id).exists():
        new_id = gen_scene_id()
    save_library_scene(new_id, data)
    return jsonify({"ok": True, "id": new_id})

@app.route("/api/show/enabled-scenes", methods=["POST"])
def api_set_enabled_scenes():
    """Update which library scenes are visible on the main screen for the active show."""
    global show_config
    enabled = request.json.get("scenes", [])
    if not isinstance(enabled, list):
        return jsonify({"ok": False, "error": "scenes must be a list"}), 400
    # Validate every id exists in library
    enabled = [sid for sid in enabled if library_path(sid).exists()]
    show_config["enabled_scenes"] = enabled
    save_json(SHOWS_DIR / config["active_show"] / "show.json", show_config)
    return jsonify({"ok": True, "scenes": enabled})

@app.route("/api/show/cycler-scenes", methods=["POST"])
def api_set_cycler_scenes():
    """Set which scenes are available in the Beat Cycler for the active show.
    Stored per-show in show.json, mirroring enabled_scenes."""
    global show_config
    ids = (request.json or {}).get("scenes", [])
    if not isinstance(ids, list):
        return jsonify({"ok": False, "error": "scenes must be a list"}), 400
    main_ids = {m["id"] for m in get_show_scenes(show_config, scene_type="main")}
    ids = [sid for sid in ids if sid in main_ids]   # keep only enabled main scenes
    show_config["cycler_scenes"] = ids
    save_json(SHOWS_DIR / config["active_show"] / "show.json", show_config)
    return jsonify({"ok": True, "scenes": ids})

@app.route("/api/show/enabled-scenes/<scene_type>", methods=["POST"])
def api_set_enabled_scenes_typed(scene_type):
    """Replace only the given type's portion of enabled_scenes, preserving every
    other type. Used by the per-section Edit-list modals and scene reorder so
    one section never drops another section's scenes from the show."""
    global show_config
    if scene_type not in ("main", "mover_motion", "mover_look", "effect"):
        return jsonify({"ok": False, "error": "bad scene_type"}), 400
    submitted = (request.json or {}).get("scenes", [])
    if not isinstance(submitted, list):
        return jsonify({"ok": False, "error": "scenes must be a list"}), 400

    def _type_of(sid):
        try:
            return load_library_scene(sid).get("scene_type", "main")
        except FileNotFoundError:
            return None

    typed  = [sid for sid in submitted if _type_of(sid) == scene_type]
    others = [sid for sid in show_config.get("enabled_scenes", [])
              if _type_of(sid) not in (scene_type, None)]
    show_config["enabled_scenes"] = others + typed
    save_json(SHOWS_DIR / config["active_show"] / "show.json", show_config)
    return jsonify({"ok": True, "scenes": typed})

@app.route("/api/library/scene/<scene_id>/export")
def api_export_scene(scene_id):
    p = library_path(scene_id)
    if not p.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(p, as_attachment=True, download_name=f"{scene_id}.json")

@app.route("/api/library/export_all")
def api_export_all():
    buf = io.BytesIO()
    files = list(LIBRARY_DIR.glob("*.json"))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(files):
            zf.write(f, f.name)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name="scene_library.zip",
                     mimetype="application/zip")

@app.route("/api/library/import", methods=["POST"])
def api_import_scenes():
    """Import scenes into the library. Imported scenes are NOT auto-enabled in any show."""
    if "files" not in request.files:
        return jsonify({"ok": False, "error": "No files uploaded"}), 400
    imported, errors = [], []

    def _save_one(data, original_name):
        # If a 'name' isn't in the data, derive from filename
        if "name" not in data:
            data["name"] = Path(original_name).stem
        if "folder" not in data:
            data["folder"] = "Imported"
        new_id = gen_scene_id()
        while library_path(new_id).exists():
            new_id = gen_scene_id()
        save_library_scene(new_id, data)
        imported.append({"id": new_id, "name": data["name"]})

    for file in request.files.getlist("files"):
        try:
            if file.filename.endswith(".zip"):
                zf = zipfile.ZipFile(io.BytesIO(file.read()))
                for name in zf.namelist():
                    if name.endswith(".json"):
                        try:
                            _save_one(json.loads(zf.read(name)), name)
                        except Exception as e:
                            errors.append({"file": name, "error": str(e)})
            elif file.filename.endswith(".json"):
                _save_one(json.loads(file.read()), file.filename)
        except Exception as e:
            errors.append({"file": file.filename, "error": str(e)})

    return jsonify({"ok": True, "imported": imported, "errors": errors})

# ── Show management ───────────────────────────────────────────────────────

def _list_shows():
    shows = []
    for d in sorted(SHOWS_DIR.iterdir()):
        sf = d / "show.json"
        if sf.exists():
            try:
                s = load_json(sf)
                shows.append({"id": d.name, "name": s.get("name", d.name)})
            except Exception:
                pass
    return shows

@app.route("/api/shows")
def api_shows():
    return jsonify(_list_shows())

@app.route("/api/shows/switch/<show_id>", methods=["POST"])
def api_switch_show(show_id):
    global show_config, scenes_dir
    try:
        new_show = load_show(show_id)
        show_config = new_show
        scenes_dir  = get_scenes_dir(show_id)
        Path(scenes_dir).mkdir(parents=True, exist_ok=True)
        # Hot-swap the show on the existing engine (no zombie threads!)
        engine.load_show(new_show)

        config["active_show"] = show_id
        save_json(CONFIG_PATH, config)

        startup = show_config.get("startup_scene")
        if startup:
            try:
                scene = load_library_scene(startup)
                engine.play_scene(scene, launch_fade_ms=2000, scene_id=startup)
            except Exception as e:
                log.warning(f"Startup scene failed: {e}")

        return jsonify({"ok": True, "show": show_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/show")
def api_show():
    return jsonify(show_config)

# ── Settings ──────────────────────────────────────────────────────────────

@app.route("/settings")
def settings():
    return render_template("settings.html",
        show=show_config,
        active_show=config["active_show"],
        shows=_list_shows(),
        scenes=list_library_scenes(),
        palette=palette,
    )

@app.route("/api/show/config", methods=["POST"])
def api_save_show_config():
    global show_config, engine
    data = request.json
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400
    show_id  = config["active_show"]
    cfg_path = SHOWS_DIR / show_id / "show.json"
    try:
        # Merge into existing config so we preserve fields that the settings page
        # doesn't manage (enabled_scenes, scene ordering, etc.). Only fields the
        # client sent get overwritten.
        existing = load_json(cfg_path) if cfg_path.exists() else {}
        existing.update(data)
        save_json(cfg_path, existing)
        show_config = existing
        engine.load_show(existing)
        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"Failed to save show config: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Color palette ─────────────────────────────────────────────────────────

@app.route("/api/palette", methods=["GET"])
def api_get_palette():
    return jsonify(palette)

@app.route("/api/palette", methods=["POST"])
def api_save_palette():
    """Replace the shared palette. Validates basic shape so a malformed POST
    can't corrupt the file the pickers depend on."""
    global palette
    data = request.json
    if not isinstance(data, dict) or "engines" not in data or "colors" not in data:
        return jsonify({"ok": False, "error": "Palette must have 'engines' and 'colors'"}), 400
    if not isinstance(data["engines"], dict) or not isinstance(data["colors"], list):
        return jsonify({"ok": False, "error": "'engines' must be an object and 'colors' a list"}), 400
    for c in data["colors"]:
        if not isinstance(c, dict) or "name" not in c or not isinstance(c.get("recipes"), dict):
            return jsonify({"ok": False, "error": "Each color needs a name and a recipes object"}), 400
    data.setdefault("version", 1)
    try:
        save_json(PALETTE_PATH, data)
        palette = data
        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"Failed to save palette: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Show create / duplicate ───────────────────────────────────────────────

import shutil, re

def slugify(name):
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_') or 'show'

@app.route("/api/shows/create", methods=["POST"])
def api_create_show():
    data      = request.json or {}
    name      = data.get("name", "New Show").strip()
    source_id = data.get("duplicate_from")
    show_id   = slugify(name)

    # Avoid name collision
    base = show_id
    n    = 1
    while (SHOWS_DIR / show_id).exists():
        show_id = f"{base}_{n}"; n += 1

    show_dir = SHOWS_DIR / show_id
    show_dir.mkdir(parents=True)
    (show_dir / "scenes").mkdir()

    if source_id and (SHOWS_DIR / source_id).exists():
        # Duplicate config from source, don't copy scenes
        src_cfg = load_show(source_id)
        src_cfg["name"] = name
        save_json(show_dir / "show.json", src_cfg)
    else:
        # Blank show
        blank = {
            "name": name,
            "startup_scene": "",
            "singer_fade_ms": 1500,
            "singer_color": {"r":20,"g":0,"b":0,"a":200,"w":220,"uv":0},
            "fixtures": []
        }
        save_json(show_dir / "show.json", blank)

    log.info(f"Created show: {show_id}")
    return jsonify({"ok": True, "id": show_id, "name": name})

@app.route("/api/shows/delete/<show_id>", methods=["DELETE"])
def api_delete_show(show_id):
    if show_id == config.get("active_show"):
        return jsonify({"ok": False, "error": "Cannot delete the active show"}), 400
    target = SHOWS_DIR / show_id
    if not target.exists():
        return jsonify({"ok": False, "error": "Show not found"}), 404
    shutil.rmtree(target)
    log.info(f"Deleted show: {show_id}")
    return jsonify({"ok": True})

# ── Cell-strip inspection (Phase 2A) ───────────────────────────────────────

@app.route("/api/cell-strips")
def api_cell_strips():
    """Inspection endpoint for the cell-strip abstraction. Returns the
    cell-strip layout for every non-mover fixture in both rendering
    modes."""
    result = []
    for fx in show_config.get("fixtures", []):
        if fx.get("type") == "mover":
            continue
        entry = {
            "fixture_id":   fx["id"],
            "fixture_name": fx.get("name", fx["id"]),
            "fixture_type": fx.get("type", "pod"),
        }
        for mode in cell_strip.ALL_MODES:
            strips = engine.get_cell_strips(fx["id"], mode=mode)
            entry[mode] = [{
                "label":      s["label"],
                "length":     s["length"],
                "color_keys": list(s["color_keys"]),
                "first_channel": s["writes"][0][0][0] if s["writes"] else None,
                "last_channel":  s["writes"][-1][-1][0] if s["writes"] else None,
            } for s in strips]
        result.append(entry)
    return jsonify(result)

# ── Effect scenes (Phase 2C) ───────────────────────────────────────────────
#
# Effect scenes are stored in the same scene_library/ as main / mover_motion
# / mover_look scenes (scene_type="effect"). The endpoints below mirror the
# motion/look playback shape.

@app.route("/api/effects/registry")
def api_effects_registry():
    """Effect catalogue + parameter schema for the editor UI (Phase 2D).
    Returns the same data as effects.get_registry()."""
    return jsonify(effects.get_registry())

@app.route("/api/effect/<scene_id>", methods=["POST"])
def api_play_effect(scene_id):
    """Play an effect scene by library id. Body may include `toggle: true`
    to make a repeat-tap stop the scene."""
    try:
        scene = load_library_scene(scene_id)
        if (scene.get("scene_type") or "main") != "effect":
            return jsonify({"ok": False, "error": "Not an effect scene"}), 400
        data = request.json or {}
        if data.get("toggle"):
            engine.toggle_effect_scene(scene, scene_id=scene_id)
        else:
            engine.play_effect_scene(scene, scene_id=scene_id)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Scene not found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/effect/stop", methods=["POST"])
def api_stop_effect():
    engine.stop_effect_scene()
    return jsonify({"ok": True})

@app.route("/api/effect/preview", methods=["POST"])
def api_effect_preview():
    """Live-preview from the editor. Body:
        {"scene": {...effect scene dict...}}   → start or hot-swap preview
        {"clear": true}                        → fade preview out
    """
    data = request.json or {}
    if data.get("clear"):
        engine.preview_effect_clear()
    elif data.get("scene") is not None:
        engine.preview_effect(data["scene"])
    else:
        return jsonify({"ok": False, "error": "Need 'scene' or 'clear'"}), 400
    return jsonify({"ok": True})


# ── Fixtures directory API ─────────────────────────────────────────────────

FIXTURES_DIR = BASE_DIR / "fixtures"
FIXTURES_DIR.mkdir(exist_ok=True)

def list_fixture_library():
    fxs = []
    for f in sorted(FIXTURES_DIR.glob("*.json")):
        try:
            fxs.append(load_json(f))
        except Exception:
            pass
    return fxs

@app.route("/api/fixtures")
def api_fixtures_list():
    return jsonify(list_fixture_library())

@app.route("/api/fixtures", methods=["POST"])
def api_fixture_save():
    data = request.json
    if not data or not data.get("id"):
        return jsonify({"ok": False, "error": "id required"}), 400
    safe_id = re.sub(r'[^a-z0-9_]', '_', data["id"].lower())
    save_json(FIXTURES_DIR / f"{safe_id}.json", data)
    return jsonify({"ok": True, "id": safe_id})

SLOT_ROLES = {"color_wheel", "gobo", "gobo2", "prism", "prism2"}

def _slot_dir(fx_id):
    import re as _re
    safe = _re.sub(r'[^a-z0-9_]', '_', str(fx_id).lower())
    return Path(app.static_folder) / "slots" / safe

@app.route("/api/fixtures/<fx_id>", methods=["DELETE"])
def api_fixture_delete(fx_id):
    import shutil as _shutil
    path = FIXTURES_DIR / f"{fx_id}.json"
    if not path.exists():
        return jsonify({"ok": False, "error": "Not found"}), 404
    path.unlink()
    sd = _slot_dir(fx_id)          # clean up this fixture's wheel-slot images
    if sd.is_dir():
        _shutil.rmtree(sd, ignore_errors=True)
    return jsonify({"ok": True})

@app.route("/api/fixtures/<fx_id>/slot-image", methods=["POST"])
def api_slot_image_save(fx_id):
    """Save a browser-downscaled wheel-slot thumbnail.
    Body: {"role","uid","dataurl"} -> {"ok","img":"<role>/<uid>.jpg"}."""
    import base64 as _b64, re as _re
    data = request.json or {}
    role = data.get("role")
    uid  = _re.sub(r'[^a-z0-9_]', '_', str(data.get("uid", "")).lower())
    durl = data.get("dataurl") or ""
    if role not in SLOT_ROLES or not uid:
        return jsonify({"ok": False, "error": "Bad role/uid"}), 400
    if "," not in durl:
        return jsonify({"ok": False, "error": "Bad image data"}), 400
    try:
        raw = _b64.b64decode(durl.split(",", 1)[1])
    except Exception:
        return jsonify({"ok": False, "error": "Decode failed"}), 400
    if len(raw) > 3_000_000:
        return jsonify({"ok": False, "error": "Image too large"}), 413
    d = _slot_dir(fx_id) / role
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{uid}.jpg").write_bytes(raw)
    return jsonify({"ok": True, "img": f"{role}/{uid}.jpg"})

@app.route("/api/fixtures/<fx_id>/slot-image", methods=["DELETE"])
def api_slot_image_delete(fx_id):
    import re as _re
    data = request.json or {}
    role = data.get("role")
    uid  = _re.sub(r'[^a-z0-9_]', '_', str(data.get("uid", "")).lower())
    if role not in SLOT_ROLES or not uid:
        return jsonify({"ok": False, "error": "Bad role/uid"}), 400
    f = _slot_dir(fx_id) / role / f"{uid}.jpg"
    if f.exists():
        f.unlink()
    return jsonify({"ok": True})


# ── Stage Visualizer ───────────────────────────────────
# Web-based stage visualizer + layout designer. Mirrors live DMX output onto a
# draggable, scalable map of the rig's fixtures. Layout persists per show in
# show.json under "stage". Requires templates/stage.html.

@app.route("/stage")
def stage():
    return render_template("stage.html",
                           show_name=show_config.get("name", "Lightboard"))


def _stage_overflow(uni, abs_ch):
    """(universe, channel) for a 1-indexed absolute channel, >512 overflow
    aware. Returns a [uni, ch] list for JSON."""
    u, c = cell_strip.ch_overflow(int(uni), int(abs_ch))
    return [u, c]


def _stage_abs(fx, offset_1indexed):
    """Absolute [uni, ch] for a 1-indexed offset within a fixture."""
    return _stage_overflow(fx.get("universe", 0),
                           int(fx.get("start_address", 1)) + int(offset_1indexed) - 1)


def _stage_geometry(fx):
    """Visualizer geometry for one fixture: id, name, type, is_mover,
    cell_count, per-cell DMX writes, and an optional global dimmer channel.
    Non-movers use the continuous cell strip (one cell per pod / per pixel);
    movers become a single colour head built from channel_roles."""
    if cell_strip.is_mover(fx):
        roles = fx.get("channel_roles") or {}
        cell  = []
        for key in ("r", "g", "b", "a", "w", "uv", "l"):
            if key in roles:
                cell.append(_stage_abs(fx, roles[key]) + [key])
        dimmer_ch = _stage_abs(fx, roles["dimmer"]) if "dimmer" in roles else None
        return {"id": fx["id"], "name": fx.get("name", fx["id"]),
                "type": fx.get("type", "mover"), "is_mover": True,
                "cell_count": 1, "cells": [cell] if cell else [[]],
                "dimmer_ch": dimmer_ch, "reverse": bool(fx.get("reverse"))}

    strips = cell_strip.build_cell_strips(fx, cell_strip.MODE_CONTINUOUS_STRIP)
    cells  = []
    if strips:
        for cell_writes in strips[0]["writes"]:
            cells.append([[u, c, key] for ((u, c), key) in cell_writes])
    dimmer_ch = _stage_abs(fx, fx["dimmer_channel"]) if fx.get("dimmer_channel") else None
    return {"id": fx["id"], "name": fx.get("name", fx["id"]),
            "type": fx.get("type", "pod"), "is_mover": False,
            "cell_count": len(cells), "cells": cells, "dimmer_ch": dimmer_ch, "reverse": bool(fx.get("reverse"))}


@app.route("/api/stage/fixtures")
def api_stage_fixtures():
    """Per-fixture geometry + DMX cell map for the visualizer."""
    return jsonify([_stage_geometry(fx) for fx in show_config.get("fixtures", [])])


@app.route("/api/stage/layout", methods=["GET"])
def api_stage_layout_get():
    """Return the saved stage layout for the active show (or {} if none)."""
    return jsonify(show_config.get("stage", {}))


@app.route("/api/stage/layout", methods=["POST"])
def api_stage_layout_set():
    """Persist the stage layout into the active show's show.json."""
    global show_config
    show_config["stage"] = request.json or {}
    save_json(SHOWS_DIR / config["active_show"] / "show.json", show_config)
    return jsonify({"ok": True})


# ── Groups & fixture reverse ─────────────────────────────

@app.route("/api/groups", methods=["GET"])
def api_groups_get():
    """List the fixture groups defined in the active show."""
    return jsonify(show_config.get("groups", []))


@app.route("/api/groups", methods=["POST"])
def api_groups_set():
    """Replace the active show's group list (the Stage page sends the whole
    list). Each group: {id, name, members: [fixture_id, ...] in render order}."""
    global show_config
    data   = request.json or {}
    groups = data.get("groups") if isinstance(data, dict) else data
    if not isinstance(groups, list):
        return jsonify({"ok": False, "error": "expected a list of groups"}), 400
    show_config["groups"] = groups
    save_json(SHOWS_DIR / config["active_show"] / "show.json", show_config)
    return jsonify({"ok": True, "groups": groups})


@app.route("/api/fixtures/<fx_id>/reverse", methods=["POST"])
def api_fixture_reverse(fx_id):
    """Set a fixture's 'reverse output' flag (pods/cells run backwards) and
    rebuild the cell-strip cache so effects and scenes pick it up live."""
    global show_config
    data  = request.json or {}
    value = bool(data.get("value", True))
    found = False
    for fx in show_config.get("fixtures", []):
        if fx.get("id") == fx_id:
            fx["reverse"] = value
            found = True
            break
    if not found:
        return jsonify({"ok": False, "error": "fixture not found"}), 404
    save_json(SHOWS_DIR / config["active_show"] / "show.json", show_config)
    engine.rebuild_cell_strips()
    return jsonify({"ok": True, "id": fx_id, "reverse": value})


# ── Run ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(
        host=config.get("web_host", "0.0.0.0"),
        port=config.get("web_port", 5000),
        debug=False,
        threaded=True,
    )
