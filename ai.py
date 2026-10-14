"""Lightboard AI helpers (Claude Platform integration).

Flask-free, like mover_gen.py. Everything here is a pure function of its
inputs plus the API key; ai_routes.py gathers show/library context under
Flask and hands it in.

Features
  * fixture_from_manual()  — PDF manual → candidate fixture profiles in the
    exact shape fixtures/*.json already uses (pod / pixel_strip / mover).
  * scenes_from_prompt()   — free-text prompt → scene_library-shaped scenes
    (effect or main), validated against the active show before return.
  * scenes_from_setlist()  — one scene per setlist line.

Design rules
  * The model never writes files and never emits raw DMX. Every call uses
    forced tool use with a JSON schema derived from the engine's own
    formats (effects.get_registry(), fixture dicts, show fixtures/groups),
    so the output can only describe things the engine can play.
  * Every result goes through validate_*() before it reaches the UI; the
    UI then saves through the normal /api/fixtures and /api/library/scene
    routes. Nothing bypasses the existing save paths.
  * API key: ANTHROPIC_API_KEY env var, else the gitignored ai_key file
    next to pi_role. Never in config.json or the repo.
  * Static context (schemas, registry, patch) is sent as a cached system
    block so repeat calls are cheap.
"""

import base64
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
KEY_FILE = BASE_DIR / "ai_key"

DEFAULT_MODEL = "claude-sonnet-5-5"
FAST_MODEL    = "claude-haiku-5-5"

DEFAULT_CFG = {
    "enabled":             True,
    "model":               DEFAULT_MODEL,
    "fixture_from_manual": True,
    "scene_from_prompt":   True,
    "messenger_control":   False,   # Phase 2 — messenger → scene actions
    "messenger_allow":     [],      # sender names allowed to drive actions
}

MAX_PDF_BYTES = 30 * 1024 * 1024   # API document limit is 32 MB

# Role vocabulary — mirrors rebuildRoleRows() in settings.html. Keep in sync.
MOVER_ROLES = [
    "pan", "pan_fine", "tilt", "tilt_fine",
    "dimmer", "shutter", "strobe",
    "r", "g", "b", "w",
    "r2", "g2", "b2", "r3", "g3", "b3",
    "color_wheel", "gobo", "gobo_rot", "gobo2", "gobo2_rot",
    "prism", "prism_rot", "prism2", "prism2_rot",
    "focus", "zoom", "iris",
    "speed",
    "effect", "intensity", "palette", "effect_option",
]
SLOT_ROLES   = ["color_wheel", "gobo", "gobo2", "prism", "prism2"]
COLOR_KEYS   = ["r", "g", "b", "a", "w", "uv", "l"]
COLOR_LABELS = {"r": "red", "g": "green", "b": "blue", "a": "amber",
                "w": "white", "uv": "UV", "l": "lime"}


# ── Config / key ───────────────────────────────────────────────────────────

def merged_cfg(cfg):
    out = dict(DEFAULT_CFG)
    if isinstance(cfg, dict):
        out.update({k: v for k, v in cfg.items() if k in DEFAULT_CFG})
    return out


def get_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if k:
        return k, "env"
    try:
        k = KEY_FILE.read_text().strip()
        if k:
            return k, "file"
    except Exception:
        pass
    return None, None


def set_key(key):
    key = (key or "").strip()
    if not key:
        try:
            KEY_FILE.unlink()
        except FileNotFoundError:
            pass
        return
    KEY_FILE.write_text(key + "\n")
    try:
        os.chmod(KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def key_status():
    k, src = get_key()
    return {
        "configured": bool(k),
        "source":     src,
        "hint":       (k[:10] + "…" + k[-4:]) if k and len(k) > 16 else None,
    }


def sdk_available():
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def status(cfg):
    c = merged_cfg(cfg)
    ks = key_status()
    return {
        "cfg":       c,
        "key":       ks,
        "sdk":       sdk_available(),
        "available": bool(c["enabled"] and ks["configured"] and sdk_available()),
    }


def _client():
    import anthropic
    key, _ = get_key()
    if not key:
        raise RuntimeError("No API key configured (Settings → AI).")
    return anthropic.Anthropic(api_key=key, max_retries=2, timeout=180.0)


def _call_tool(model, system_blocks, user_content, tool, max_tokens=8000):
    """One forced-tool-use round trip. Returns (tool_input, usage_dict)."""
    client = _client()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user_content}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
    )
    tool_input = None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
            tool_input = block.input
            break
    if tool_input is None:
        raise RuntimeError("Model returned no structured result (stop_reason=%s)"
                           % getattr(resp, "stop_reason", "?"))
    u = resp.usage
    usage = {
        "input":        getattr(u, "input_tokens", 0),
        "output":       getattr(u, "output_tokens", 0),
        "cache_read":   getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write":  getattr(u, "cache_creation_input_tokens", 0) or 0,
        "model":        model,
    }
    return tool_input, usage


def _cached(text):
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def test_connection(cfg):
    """Cheapest possible round trip to prove the key works."""
    client = _client()
    t0 = time.time()
    resp = client.messages.create(
        model=FAST_MODEL, max_tokens=8,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )
    txt = "".join(getattr(b, "text", "") for b in resp.content)
    return {"ok": True, "reply": txt.strip(), "ms": int((time.time() - t0) * 1000),
            "model": FAST_MODEL}


# ── Job runner ─────────────────────────────────────────────────────────────

class Jobs:
    """Tiny in-memory job table so API calls never block a Flask worker or
    the engine tick. One thread per job; results kept for ~1 h."""

    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def start(self, kind, fn, *args, **kwargs):
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, "status": "running", "t0": time.time(),
               "result": None, "error": None}
        with self._lock:
            self._jobs[jid] = job
            self._prune()
        def run():
            try:
                job["result"] = fn(*args, **kwargs)
                job["status"] = "done"
            except Exception as e:                       # noqa: BLE001
                log.exception("AI job %s (%s) failed", jid, kind)
                job["error"]  = _friendly_error(e)
                job["status"] = "error"
            job["t1"] = time.time()
        threading.Thread(target=run, name=f"ai-{kind}-{jid}", daemon=True).start()
        return jid

    def get(self, jid):
        with self._lock:
            j = self._jobs.get(jid)
        if not j:
            return None
        out = {k: j[k] for k in ("id", "kind", "status", "error", "result")}
        out["elapsed"] = round((j.get("t1") or time.time()) - j["t0"], 1)
        return out

    def _prune(self):
        cutoff = time.time() - 3600
        for k in [k for k, j in self._jobs.items() if j["t0"] < cutoff]:
            self._jobs.pop(k, None)


def _friendly_error(e):
    name = type(e).__name__
    msg  = str(e)
    if "credit" in msg.lower() or "billing" in msg.lower():
        return "API credit balance exhausted or billing issue — check platform.claude.com."
    if name in ("AuthenticationError",) or "401" in msg:
        return "API key rejected (401). Re-enter it in Settings → AI."
    if name in ("RateLimitError",) or "429" in msg:
        return "Rate limited (429) — wait a moment and retry."
    if name in ("APIConnectionError", "APITimeoutError"):
        return "Could not reach the Claude API — check the Pi's internet (wlan0)."
    return f"{name}: {msg}"[:400]


# ── Feature 1: fixture profile from manual ─────────────────────────────────

_FIXTURE_TOOL = {
    "name": "save_fixture_profiles",
    "description": (
        "Record every DMX personality (mode) documented in the manual as a "
        "Lightboard fixture profile. One profile per DMX mode. Channel numbers "
        "are 1-based within the fixture's own footprint."),
    "input_schema": {
        "type": "object",
        "properties": {
            "manufacturer": {"type": "string"},
            "model":        {"type": "string"},
            "profiles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "mode_name": {"type": "string",
                                      "description": "Mode label as printed in the manual, e.g. '13CH', 'TOUR', '54ch 8-zone'."},
                        "type": {"type": "string", "enum": ["pod", "pixel_strip", "mover"],
                                 "description": "pod = par/bar/wash with one or more RGB-ish cells and no pan/tilt; pixel_strip = long addressable strip/tube (dozens of pixels); mover = anything with pan/tilt or wheels."},
                        "channels": {"type": "integer", "minimum": 1, "maximum": 32768,
                                     "description": "Total DMX footprint of this mode."},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "notes": {"type": "string",
                                  "description": "Anything the schema can't express: per-pod dimmers, macro/strobe channels left unmapped, ambiguities."},
                        # pod
                        "pods": {"type": "integer", "minimum": 1, "maximum": 64,
                                 "description": "pod type: number of independently-addressable colour cells/zones."},
                        "dimmer_channel": {"type": "integer", "minimum": 0,
                                           "description": "Master dimmer channel (1-based). 0 if the mode has none."},
                        "first_pod_channel": {"type": "integer", "minimum": 1,
                                              "description": "pod type: channel where cell 1's colour block starts."},
                        "channels_per_pod": {"type": "integer", "minimum": 1, "maximum": 20},
                        "pod_color_offsets": {
                            "type": "object",
                            "description": "pod type: 0-based offset of each colour within a cell's block. Keys: r g b a w uv l (l = lime).",
                            "additionalProperties": {"type": "integer", "minimum": 0}},
                        # pixel_strip
                        "pixel_count":         {"type": "integer", "minimum": 1},
                        "first_pixel_channel": {"type": "integer", "minimum": 1},
                        "channels_per_pixel":  {"type": "integer", "minimum": 1, "maximum": 8},
                        "pixel_color_offsets": {"type": "object",
                                                "additionalProperties": {"type": "integer", "minimum": 0}},
                        # mover
                        "channel_roles": {
                            "type": "object",
                            "description": "mover type: role → 1-based channel. Allowed roles: " + ", ".join(MOVER_ROLES),
                            "additionalProperties": {"type": "integer", "minimum": 1}},
                        "channel_slots": {
                            "type": "object",
                            "description": "mover type: value ranges for wheel channels (color_wheel, gobo, gobo2, prism, prism2) from the manual's tables.",
                            "additionalProperties": {
                                "type": "array",
                                "items": {"type": "object",
                                          "properties": {"label": {"type": "string"},
                                                         "lo": {"type": "integer", "minimum": 0, "maximum": 255},
                                                         "hi": {"type": "integer", "minimum": 0, "maximum": 255}},
                                          "required": ["label", "lo", "hi"]}}},
                    },
                    "required": ["mode_name", "type", "channels", "confidence"],
                },
            },
        },
        "required": ["profiles"],
    },
}

_FIXTURE_SYSTEM = """You convert DMX fixture manuals into Lightboard fixture profiles.

Lightboard profile shapes (these are the real on-disk formats — match them exactly):

pod (pars, bars, washes — colour cells, no pan/tilt):
  {"type":"pod","channels":13,"pods":1,"dimmer_channel":1,"first_pod_channel":2,
   "channels_per_pod":6,"pod_color_offsets":{"r":0,"g":1,"b":2,"a":3,"w":4,"uv":5}}
  Multi-zone bar example (4 zones × RGBAWUV after a master dimmer):
  {"type":"pod","channels":30,"pods":4,"dimmer_channel":1,"first_pod_channel":2,
   "channels_per_pod":6,"pod_color_offsets":{"r":0,"g":1,"b":2,"a":3,"w":4,"uv":5}}
  Fixture with no master dimmer, colour block starting at ch1, with lime:
  {"type":"pod","channels":7,"pods":1,"dimmer_channel":0,"first_pod_channel":1,
   "channels_per_pod":7,"pod_color_offsets":{"r":0,"g":1,"b":2,"l":3,"a":4,"uv":5}}

pixel_strip (addressable tubes/strips, many pixels):
  {"type":"pixel_strip","channels":756,"pixel_count":189,"dimmer_channel":0,
   "first_pixel_channel":1,"channels_per_pixel":4,"pixel_color_offsets":{"r":0,"g":1,"b":2,"w":3}}

mover (anything with pan/tilt or wheels):
  {"type":"mover","channels":16,"channel_roles":{"pan":1,"pan_fine":2,"tilt":3,"tilt_fine":4,
   "speed":5,"dimmer":6,"strobe":7,"color_wheel":8,"gobo":9,"gobo2":10,"gobo2_rot":11,"focus":12,"prism":13},
   "channel_slots":{"color_wheel":[{"label":"White","lo":0,"hi":9},{"label":"Red","lo":10,"hi":19}]}}

Rules:
- Produce one profile per DMX mode/personality the manual documents. Mode names as printed.
- pod: pods × channels_per_pod cells must all fit inside `channels`, starting at first_pod_channel.
  Only count real colour emitters (r g b a w uv l) in pod_color_offsets; strobe/macro/speed channels
  inside the block are simply not listed as offsets (channels_per_pod still counts them).
- If a bar lists a master dimmer AND per-zone dimmers, use the master as dimmer_channel and mention
  the per-zone dimmers in notes (the schema has no per-pod dimmer).
- mover: only use roles from the allowed list. Fine/16-bit channels map to pan_fine/tilt_fine.
  Fill channel_slots from the range tables for wheel channels when present; split 'rotation' ranges
  into their own labelled rows. Skip reset/lamp/macro channels unless a role exists.
- Set confidence < 0.6 whenever a table was ambiguous, cut across pages, or you inferred layout.
- Never invent channels that are not in the document."""


def _pdf_block(pdf_bytes):
    return {"type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.b64encode(pdf_bytes).decode("ascii")}}


def _slug(s):
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s[:48] or "fixture"


def fixture_from_manual(pdf_bytes, filename, hint, cfg):
    """→ {"manufacturer","model","profiles":[validated profile dicts],
          "rejected":[{mode_name,errors}], "usage":{...}}"""
    c = merged_cfg(cfg)
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError(f"PDF too large ({len(pdf_bytes)//1048576} MB > 30 MB)")
    user = [_pdf_block(pdf_bytes),
            {"type": "text", "text":
             f"Manual file: {filename}\n"
             + (f"Operator hint: {hint}\n" if hint else "")
             + "Extract every DMX mode into Lightboard profiles."}]
    data, usage = _call_tool(c["model"], [_cached(_FIXTURE_SYSTEM)], user,
                             _FIXTURE_TOOL, max_tokens=12000)
    mfr   = (data.get("manufacturer") or "").strip()
    model = (data.get("model") or "").strip()
    base  = _slug(f"{mfr} {model}") if (mfr or model) else _slug(Path(filename).stem)
    good, bad = [], []
    for p in data.get("profiles") or []:
        prof, errs, warns = validate_fixture_profile(p, base, mfr, model)
        if errs:
            bad.append({"mode_name": p.get("mode_name"), "errors": errs})
        else:
            prof["_warnings"]   = warns
            prof["_confidence"] = float(p.get("confidence", 0.5))
            prof["_notes"]      = (p.get("notes") or "").strip()
            good.append(prof)
    return {"manufacturer": mfr, "model": model, "profiles": good,
            "rejected": bad, "usage": usage}


def validate_fixture_profile(p, base_id, mfr="", model=""):
    """Normalise a model-proposed profile into the exact fixtures/*.json shape.
    Returns (profile, errors, warnings). errors non-empty → unusable."""
    errs, warns = [], []
    t = p.get("type")
    try:
        channels = int(p.get("channels") or 0)
    except (TypeError, ValueError):
        channels = 0
    if channels < 1:
        errs.append("channels missing")
    mode = (p.get("mode_name") or f"{channels}ch").strip()
    name = " ".join(x for x in (mfr, model) if x).strip() or base_id.replace("_", " ")
    name = f"{name} ({mode})"
    fid  = f"{base_id}_{_slug(mode)}"
    prof = {"id": fid, "name": name, "type": t, "channels": channels}

    def _int(k, default=None, lo=0):
        v = p.get(k, default)
        try:
            v = int(v)
        except (TypeError, ValueError):
            return default
        return max(lo, v)

    if t == "pod":
        pods  = _int("pods", 1, 1)
        dim   = _int("dimmer_channel", 0, 0)
        first = _int("first_pod_channel", 1, 1)
        cpp   = _int("channels_per_pod", 0, 1) or 0
        offs  = {k: int(v) for k, v in (p.get("pod_color_offsets") or {}).items()
                 if k in COLOR_KEYS and isinstance(v, (int, float))}
        if cpp < 1:
            errs.append("channels_per_pod missing")
        if not offs:
            errs.append("no colour offsets")
        if any(o >= cpp for o in offs.values()):
            errs.append("colour offset outside pod block")
        if dim > channels:
            errs.append("dimmer_channel beyond footprint")
        if cpp and first - 1 + pods * cpp > channels:
            # common model slip: counted pods × cpp over the footprint — try to recover
            fit = (channels - (first - 1)) // cpp if cpp else 0
            if fit >= 1:
                warns.append(f"pods reduced {pods}→{fit} to fit {channels}ch")
                pods = fit
            else:
                errs.append("pod block does not fit in footprint")
        if dim and first <= dim < first + pods * cpp and not errs:
            warns.append("dimmer channel sits inside the pod block — check the table")
        prof.update({"pods": pods, "dimmer_channel": dim, "first_pod_channel": first,
                     "channels_per_pod": cpp, "pod_color_offsets": offs})
    elif t == "pixel_strip":
        pc    = _int("pixel_count", 0, 1) or 0
        dim   = _int("dimmer_channel", 0, 0)
        first = _int("first_pixel_channel", 1, 1)
        cpx   = _int("channels_per_pixel", 0, 1) or 0
        offs  = {k: int(v) for k, v in (p.get("pixel_color_offsets") or {}).items()
                 if k in COLOR_KEYS and isinstance(v, (int, float))}
        if pc < 1 or cpx < 1:
            errs.append("pixel_count / channels_per_pixel missing")
        if not offs:
            errs.append("no colour offsets")
        if cpx and first - 1 + pc * cpx > channels:
            errs.append("pixel block does not fit in footprint")
        prof.update({"pixel_count": pc, "dimmer_channel": dim, "first_pixel_channel": first,
                     "channels_per_pixel": cpx, "pixel_color_offsets": offs,
                     "segments": [{"name": "Strip", "start": 0, "length": pc, "reversed": False}]})
    elif t == "mover":
        roles_in = p.get("channel_roles") or {}
        roles, dropped = {}, []
        for r, ch in roles_in.items():
            try:
                ch = int(ch)
            except (TypeError, ValueError):
                continue
            if r in MOVER_ROLES and 1 <= ch <= channels:
                roles[r] = ch
            else:
                dropped.append(r)
        if dropped:
            warns.append("unknown/out-of-range roles dropped: " + ", ".join(dropped))
        if not roles:
            errs.append("no usable channel roles")
        used = {}
        for r, ch in roles.items():
            if ch in used:
                warns.append(f"channel {ch} mapped twice ({used[ch]}, {r})")
            used[ch] = r
        slots = {}
        for sr, rows in (p.get("channel_slots") or {}).items():
            if sr not in SLOT_ROLES or sr not in roles or not isinstance(rows, list):
                continue
            out = []
            for i, row in enumerate(rows):
                try:
                    lo, hi = int(row["lo"]), int(row["hi"])
                except Exception:
                    continue
                lo, hi = max(0, min(255, lo)), max(0, min(255, hi))
                if hi < lo:
                    lo, hi = hi, lo
                out.append({"uid": f"ai{int(time.time()*1000)%100000000}{i:02d}",
                            "label": str(row.get("label") or f"{lo}-{hi}")[:40],
                            "lo": lo, "hi": hi, "img": None,
                            "_v": int(time.time() * 1000)})
            if out:
                slots[sr] = out
        prof.update({"channel_roles": roles, "channel_slots": slots})
    else:
        errs.append(f"unknown type {t!r}")
    return prof, errs, warns


# ── Feature 2: scenes from prompt ──────────────────────────────────────────

def _color_schema(desc):
    return {"type": "object", "description": desc,
            "properties": {k: {"type": "integer", "minimum": 0, "maximum": 255} for k in COLOR_KEYS},
            "additionalProperties": False}


def _scene_tool(registry, fixture_ids, group_ids):
    effect_ids = sorted(registry.keys())
    color = _color_schema("Colour as emitter levels 0-255. Keys: r g b a(amber) w(white) uv l(lime). "
                          "Include only the emitters you want lit; omit keys the mood doesn't need.")
    return {
        "name": "save_scenes",
        "description": "Return the requested scenes in Lightboard's scene format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":   {"type": "string", "description": "Short, evocative, ≤ 32 chars."},
                            "folder": {"type": "string"},
                            "scene_type": {"type": "string", "enum": ["effect", "main"]},
                            "rationale": {"type": "string", "description": "One sentence: why this fits the prompt."},
                            "launch_fade":   {"type": "integer", "minimum": 0, "maximum": 20000},
                            "tempo_sync":    {"type": "boolean"},
                            "beat_division": {"type": "number", "enum": [0.25, 0.5, 1, 2, 4]},
                            # effect scenes
                            "effect": {"type": "string", "enum": effect_ids},
                            "primary": color, "secondary": color, "accent": color,
                            "params": {"type": "object", "additionalProperties": {"type": "number"}},
                            "fixtures_enabled": {"type": "array", "items": {"type": "string", "enum": fixture_ids}},
                            "target_groups": {"type": "array", "items": {"type": "string", "enum": group_ids}} if group_ids
                                             else {"type": "array", "items": {"type": "string"}},
                            "rendering_mode": {"type": "string", "enum": ["lock_to_segments", "continuous_strip"]},
                            # main scenes
                            "steps": {
                                "type": "array",
                                "description": "main scenes only. Each step = static look held then crossfaded to the next.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "fade": {"type": "integer", "minimum": 0, "maximum": 30000},
                                        "hold": {"type": "integer", "minimum": 0, "maximum": 600000},
                                        "fixtures": {
                                            "type": "object",
                                            "description": "fixture id → look. Use `color` for a uniform fixture, or `colors`+`pattern` to alternate across cells/zones.",
                                            "additionalProperties": {
                                                "type": "object",
                                                "properties": {
                                                    "dimmer": {"type": "integer", "minimum": 0, "maximum": 255},
                                                    "color":  color,
                                                    "colors": {"type": "array", "items": color, "maxItems": 4},
                                                    "pattern": {"type": "array",
                                                                "description": "index into `colors` per cell/zone, cycled to the fixture's cell count; -1 = cell off",
                                                                "items": {"type": "integer", "minimum": -1, "maximum": 3}},
                                                },
                                            },
                                        },
                                    },
                                    "required": ["fixtures"],
                                },
                            },
                        },
                        "required": ["name", "scene_type", "rationale"],
                    },
                },
            },
            "required": ["scenes"],
        },
    }


_SCENE_SYSTEM = """You design lighting scenes for Lightboard, a live-show DMX controller.

Two scene types:
- effect: one animated effect over a set of fixtures. Fields: effect (from the registry), primary /
  secondary / accent colours (only the slots the effect uses), params (within the registry ranges),
  fixtures_enabled (fixture ids), rendering_mode: lock_to_segments (each bar/zone group animates on
  its own) or continuous_strip (all enabled fixtures joined into one long strip). Beat-locked effects
  use tempo_sync true + beat_division.
- main: static looks in steps. One step = a still picture. Several steps = a slow crossfading loop.
  Movers are never in main scenes. For multi-cell bars use colors + pattern to alternate.

Colour recipes use emitter levels, not sRGB: warm amber = a:220 r:60; UV pink = uv:255 r:120 b:80;
cool white = w:255 b:40; lime on fixtures that have it (l) reads as punchy green-yellow; deep blue =
b:255; sunset = r:255 a:160; keep w low when you want saturated colour. Not every fixture has every
emitter — the patch lists each fixture's emitters; unsupported keys are dropped silently.

Taste: match the operator's existing scenes (examples follow) — launch_fade 500 for effects,
1000-2500 ms fades for slow main loops, tempo_sync on for anything rhythmic. Vary your candidates
meaningfully (different effect / palette / fixture subset), not three near-duplicates."""


def _patch_context(show_cfg, registry, examples):
    fx_lines = []
    for fx in show_cfg.get("fixtures", []):
        t = fx.get("type") or ("pixel_strip" if fx.get("pixel_count") else "pod")
        if t == "mover":
            fx_lines.append(f"- {fx['id']}  \"{fx.get('name','')}\"  mover (not usable in scenes here)")
            continue
        if t == "pixel_strip":
            segs = fx.get("segments") or []
            cells = len(segs) if segs else 1
            em = "".join(k for k in COLOR_KEYS if k in (fx.get("pixel_color_offsets") or {}))
            fx_lines.append(f"- {fx['id']}  \"{fx.get('name','')}\"  pixel_strip, {fx.get('pixel_count')} px in {cells} segment(s), emitters {em}")
        else:
            em = "".join(k for k in COLOR_KEYS if k in (fx.get("pod_color_offsets") or {}))
            fx_lines.append(f"- {fx['id']}  \"{fx.get('name','')}\"  pod, {fx.get('pods',1)} cell(s), emitters {em}")
    grp_lines = [f"- {g.get('id')}  \"{g.get('name','')}\"  members: {', '.join(g.get('members', []))}"
                 for g in show_cfg.get("groups", [])]
    reg = {}
    for eid, info in registry.items():
        reg[eid] = {"desc": info.get("description"),
                    "uses": [s for s in ("primary", "secondary", "accent") if info.get("uses_" + s, s == "primary")],
                    "params": {pn: {k: ps.get(k) for k in ("min", "max", "default", "unit", "label") if ps.get(k) is not None}
                               for pn, ps in info.get("params", {}).items()}}
    ex = []
    for e in examples:
        ex.append(json.dumps(e, separators=(",", ":")))
    return ("PATCH (fixture ids are exact — use them verbatim):\n" + "\n".join(fx_lines) +
            "\n\nGROUPS:\n" + ("\n".join(grp_lines) or "(none)") +
            "\n\nEFFECT REGISTRY:\n" + json.dumps(reg, separators=(",", ":")) +
            "\n\nEXAMPLES OF THE OPERATOR'S OWN SCENES:\n" + "\n".join(ex))


def _compact_example(scene):
    """Strip a library scene down for the prompt (keeps the shape, drops noise)."""
    s = {k: v for k, v in scene.items()
         if k in ("name", "scene_type", "effect", "primary", "secondary", "accent", "params",
                  "fixtures_enabled", "rendering_mode", "target_groups", "launch_fade",
                  "tempo_sync", "beat_division", "folder")}
    if scene.get("scene_type", "main") == "main":
        steps = []
        for st in (scene.get("steps") or [])[:2]:
            fxs = {}
            for fid, fd in list(st.get("fixtures", {}).items())[:3]:
                pods = fd.get("pods") or fd.get("segments") or []
                fxs[fid] = {"dimmer": fd.get("dimmer", 255), "pods": pods[:4] + (["…"] if len(pods) > 4 else [])}
            steps.append({"fade": st.get("fade"), "hold": st.get("hold"), "fixtures": fxs})
        s["steps"] = steps
    return s


def pick_examples(library_scenes, load_fn, max_n=4):
    """Choose a few real scenes as style anchors: newest effect + main scenes."""
    out, seen = [], set()
    for want in ("effect", "main"):
        for meta in reversed(library_scenes):
            if meta.get("scene_type", "main") != want or meta["id"].startswith("sample_"):
                continue
            try:
                out.append(_compact_example(load_fn(meta["id"])))
            except Exception:
                continue
            seen.add(meta["id"])
            if sum(1 for e in out if e.get("scene_type") == want) >= 2:
                break
    return out[:max_n]


def scenes_from_prompt(prompt, n, scene_type, show_cfg, registry, examples, cfg,
                       folder="AI drafts"):
    """→ {"scenes":[validated scene dicts], "rejected":[...], "usage":{...}}"""
    c = merged_cfg(cfg)
    n = max(1, min(6, int(n or 3)))
    fixture_ids = [fx["id"] for fx in show_cfg.get("fixtures", []) if fx.get("type") != "mover"]
    group_ids   = [g["id"] for g in show_cfg.get("groups", []) if g.get("id")]
    if not fixture_ids:
        raise ValueError("The active show has no non-mover fixtures to light.")
    tool = _scene_tool(registry, fixture_ids, group_ids)
    want = {"effect": "effect scenes", "main": "main (static/step) scenes"}.get(scene_type,
            "scenes (choose effect or main per candidate, whichever fits best)")
    user = [{"type": "text", "text":
             f"Create {n} distinct {want} for this request. Put them in folder \"{folder}\".\n\n"
             f"REQUEST: {prompt.strip()}"}]
    data, usage = _call_tool(c["model"],
                             [_cached(_SCENE_SYSTEM + "\n\n" + _patch_context(show_cfg, registry, examples))],
                             user, tool, max_tokens=8000)
    good, bad = [], []
    for s in data.get("scenes") or []:
        scene, errs, warns = validate_scene(s, show_cfg, registry, folder)
        if errs:
            bad.append({"name": s.get("name"), "errors": errs})
        else:
            scene["_warnings"]  = warns
            scene["_rationale"] = (s.get("rationale") or "").strip()
            good.append(scene)
    return {"scenes": good, "rejected": bad, "usage": usage}


def scenes_from_setlist(setlist_text, style, show_cfg, registry, examples, cfg,
                        folder="Setlist"):
    """One scene per non-empty line. Lines may be 'Song — vibe words'."""
    lines = [ln.strip() for ln in (setlist_text or "").splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Setlist is empty.")
    if len(lines) > 30:
        raise ValueError("Setlist limited to 30 songs per run.")
    numbered = "\n".join(f"{i+1:02d}. {ln}" for i, ln in enumerate(lines))
    prompt = ("SETLIST — produce exactly one scene per line, in order, and name each scene "
              "'NN Song title' using the line's number and title (≤ 32 chars). Choose effect or main "
              "per song based on the vibe words; ballads → slow main crossfades, up-tempo → "
              "tempo-synced effects. Vary palettes across the set so consecutive songs don't look alike.\n"
              + (f"Overall style for the night: {style.strip()}\n" if style and style.strip() else "")
              + "\n" + numbered)
    res = scenes_from_prompt(prompt, len(lines), "auto", show_cfg, registry, examples, cfg,
                             folder=folder)
    res["lines"] = lines
    return res


# ── Scene validation ───────────────────────────────────────────────────────

def _clamp_color(c, emitters=None):
    out = {}
    if not isinstance(c, dict):
        return out
    for k, v in c.items():
        if k not in COLOR_KEYS:
            continue
        if emitters is not None and k not in emitters:
            continue
        try:
            out[k] = max(0, min(255, int(round(float(v)))))
        except (TypeError, ValueError):
            pass
    return out


def _fx_cells(fx):
    t = fx.get("type") or ("pixel_strip" if fx.get("pixel_count") else "pod")
    if t == "pixel_strip":
        return max(1, len(fx.get("segments") or [])), "segments"
    return max(1, int(fx.get("pods") or 1)), "pods"


def _fx_emitters(fx):
    offs = fx.get("pod_color_offsets") or fx.get("pixel_color_offsets") or {}
    return [k for k in COLOR_KEYS if k in offs]


def validate_scene(s, show_cfg, registry, default_folder):
    """Normalise a model-proposed scene into scene_library shape.
    Returns (scene, errors, warnings)."""
    errs, warns = [], []
    fx_by_id = {fx["id"]: fx for fx in show_cfg.get("fixtures", [])}
    grp_ids  = {g.get("id") for g in show_cfg.get("groups", [])}
    st = s.get("scene_type")
    name = (s.get("name") or "Untitled").strip()[:40]
    scene = {"name": name, "scene_type": st,
             "folder": (s.get("folder") or default_folder).strip()[:40],
             "launch_fade": int(s.get("launch_fade", 500) or 0)}
    if "tempo_sync" in s:
        scene["tempo_sync"] = bool(s["tempo_sync"])
        bd = s.get("beat_division", 1)
        scene["beat_division"] = bd if bd in (0.25, 0.5, 1, 2, 4) else 1

    if st == "effect":
        eid = s.get("effect")
        info = registry.get(eid)
        if not info:
            return scene, [f"unknown effect {eid!r}"], warns
        scene["effect"] = eid
        # colour slots
        all_em = set()
        for fx in fx_by_id.values():
            all_em.update(_fx_emitters(fx))
        for slot in ("primary", "secondary", "accent"):
            uses = info.get("uses_" + slot, slot == "primary")
            col = _clamp_color(s.get(slot)) if uses else None
            scene[slot] = col if col else None
            if uses and slot == "primary" and not col:
                scene[slot] = {"r": 255, "g": 255, "b": 255}
                warns.append("primary colour missing — defaulted to white")
            scene[slot + "_sample"] = False
        # params: clamp to registry ranges
        params = {}
        spec = info.get("params", {})
        for pn, ps in spec.items():
            v = (s.get("params") or {}).get(pn, ps.get("default"))
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = ps.get("default", 0)
            lo, hi = ps.get("min"), ps.get("max")
            cv = v
            if lo is not None: cv = max(lo, cv)
            if hi is not None: cv = min(hi, cv)
            if cv != v:
                warns.append(f"{pn} clamped {v}→{cv}")
            params[pn] = int(cv) if ps.get("type") == "int" else cv
        unknown = set((s.get("params") or {}).keys()) - set(spec.keys())
        if unknown:
            warns.append("unknown params dropped: " + ", ".join(sorted(unknown)))
        scene["params"] = params
        # fixtures
        req = s.get("fixtures_enabled") or []
        enabled, dropped = [], []
        for fid in req:
            fx = fx_by_id.get(fid)
            if fx and fx.get("type") != "mover":
                enabled.append(fid)
            else:
                dropped.append(fid)
        # expand groups into fixtures if the model only gave groups
        tgs = [g for g in (s.get("target_groups") or []) if g in grp_ids]
        if not enabled and tgs:
            members = {}
            for g in show_cfg.get("groups", []):
                if g.get("id") in tgs:
                    for m in g.get("members", []):
                        if m in fx_by_id and fx_by_id[m].get("type") != "mover":
                            members[m] = True
            enabled = list(members)
        if dropped:
            warns.append("unknown/mover fixtures dropped: " + ", ".join(dropped))
        if not enabled:
            return scene, ["no valid fixtures enabled"], warns
        scene["fixtures_enabled"] = enabled
        scene["target_groups"] = tgs
        scene["target_group"]  = None
        rm = s.get("rendering_mode")
        scene["rendering_mode"] = rm if rm in ("lock_to_segments", "continuous_strip") else "lock_to_segments"
        return scene, errs, warns

    if st == "main":
        steps_in = s.get("steps") or []
        if not steps_in:
            return scene, ["main scene has no steps"], warns
        steps = []
        for st_in in steps_in[:12]:
            fx_out = {}
            for fid, look in (st_in.get("fixtures") or {}).items():
                fx = fx_by_id.get(fid)
                if not fx or fx.get("type") == "mover" or not isinstance(look, dict):
                    warns.append(f"step fixture skipped: {fid}")
                    continue
                n, key = _fx_cells(fx)
                em = _fx_emitters(fx)
                cells = []
                if look.get("colors") and look.get("pattern"):
                    cols = [_clamp_color(c, em) for c in look["colors"][:4]]
                    pat  = [int(i) for i in look["pattern"] if isinstance(i, (int, float))] or [0]
                    for i in range(n):
                        idx = pat[i % len(pat)]
                        cells.append(None if idx < 0 or idx >= len(cols)
                                     else dict(cols[idx]))
                else:
                    col = _clamp_color(look.get("color") or (look.get("colors") or [{}])[0], em)
                    cells = [dict(col) for _ in range(n)]
                if not any(c for c in cells):
                    # a fixture that's entirely off: write zeros so it actually goes dark
                    cells = [{k: 0 for k in em} for _ in range(n)]
                try:
                    dim = max(0, min(255, int(look.get("dimmer", 255))))
                except (TypeError, ValueError):
                    dim = 255
                # `pods` is what the editor writes for BOTH pod fixtures and
                # pixel strips (engine.resolve_step accepts it as the
                # per-segment alias), so use it everywhere for editor compat.
                fx_out[fid] = {"dimmer": dim, "pods": cells}
            if not fx_out:
                continue
            steps.append({"fade": int(st_in.get("fade", 1500) or 0),
                          "hold": int(st_in.get("hold", 8000) or 0),
                          "fixtures": fx_out})
        if not steps:
            return scene, ["no usable steps"], warns
        scene["steps"] = steps
        return scene, errs, warns

    return scene, [f"unsupported scene_type {st!r}"], warns
