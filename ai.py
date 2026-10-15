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
    """One tool-use round trip; returns (tool_input, usage_dict).

    Current models reject forced tool_choice ("tool"/"any" → 400), so the tool
    is offered with tool_choice=auto and the system prompt instructs the model
    to always answer by calling it. If it replies in prose anyway, one retry
    nudges it; a second miss raises."""
    client = _client()
    system = list(system_blocks) + [{"type": "text", "text":
        f"Respond ONLY by calling the `{tool['name']}` tool. Never answer in prose."}]
    messages = [{"role": "user", "content": user_content}]
    usage_total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    resp = None
    for attempt in range(2):
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages,
            tools=[tool], tool_choice={"type": "auto"},
        )
        u = resp.usage
        usage_total["input"]       += getattr(u, "input_tokens", 0) or 0
        usage_total["output"]      += getattr(u, "output_tokens", 0) or 0
        usage_total["cache_read"]  += getattr(u, "cache_read_input_tokens", 0) or 0
        usage_total["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise RuntimeError("The answer was too long for one reply (hit the output limit) — "
                               "ask for fewer fixtures or a simpler pattern, or split it into two commands.")
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
                usage_total["model"] = model
                return block.input, usage_total
        if attempt == 0:
            prose = "".join(getattr(b, "text", "") for b in resp.content).strip()
            log.warning("model answered in prose instead of calling %s — retrying once: %r",
                        tool["name"], prose[:200])
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content":
                             f"Call the `{tool['name']}` tool now with your answer."})
    raise RuntimeError("Model returned no structured result (stop_reason=%s)"
                       % getattr(resp, "stop_reason", "?"))



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
    # SDK API errors carry the server's own message in .body — surface that,
    # it says exactly which field was rejected.
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(inner, dict) and inner.get("message"):
            msg = f"{inner.get('type', 'api_error')}: {inner['message']}"
            log.error("Claude API %s (%s): %s", name, getattr(e, "status_code", "?"), inner["message"])
    if "credit" in msg.lower() or "billing" in msg.lower():
        return "API credit balance exhausted or billing issue — check platform.claude.com."
    if name in ("AuthenticationError",) or "401" in msg:
        return "API key rejected (401). Re-enter it in Settings → AI."
    if name in ("RateLimitError",) or "429" in msg:
        return "Rate limited (429) — wait a moment and retry."
    if name in ("APIConnectionError", "APITimeoutError"):
        return "Could not reach the Claude API — check the Pi's internet (wlan0)."
    return f"{name}: {msg}"[:600]


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


def _scene_item_schema(registry, fixture_ids, group_ids):
    """JSON schema for ONE scene (effect or main) — shared by the Generate
    sheet's save_scenes tool and the command tool's create_scene action."""
    effect_ids = sorted(registry.keys())
    color = _color_schema("Colour as emitter levels 0-255. Keys: r g b a(amber) w(white) uv l(lime). "
                          "Include only the emitters you want lit; omit keys the mood doesn't need.")
    return {
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
                    }


def _scene_tool(registry, fixture_ids, group_ids):
    return {
        "name": "save_scenes",
        "description": "Return the requested scenes in Lightboard's scene format.",
        "input_schema": {
            "type": "object",
            "properties": {"scenes": {"type": "array",
                                      "items": _scene_item_schema(registry, fixture_ids, group_ids)}},
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


# ── Feature 3: natural-language commands (Stage Messenger → lights) ────────
#
# One Haiku round trip turns "!black out the bars, slower" into a short list
# of actions; execute_actions() applies them through the engine's own
# hot-swap / play / stop calls. Live-only: running scenes are mutated in the
# engine, the library file is never written. A snapshot of the running state
# is taken before every command so "undo" can put it back.

COMMAND_MODEL = None     # None → cfg["model"] (Sonnet by default); set to FAST_MODEL for Haiku

_ACTION_KINDS = ["launch_scene", "stop_scene", "stop_all_effects", "stop_all_main",
                 "blackout", "blackout_off", "blackout_fixtures", "restore_fixtures",
                 "set_param", "scale_param", "set_color", "swap_effect",
                 "set_master", "set_dimmer", "create_scene", "save_scene",
                 "singer_mode", "singer_level", "singer_color", "freeze", "overlay", "undo", "reply"]

def _command_tool(registry, fixture_ids, group_ids):
    t = json.loads(json.dumps(_COMMAND_TOOL))
    t["input_schema"]["properties"]["actions"]["items"]["properties"]["scene"] = \
        _scene_item_schema(registry, fixture_ids, group_ids)
    return t


_COMMAND_TOOL = {
    "name": "lighting_actions",
    "description": "Apply the operator's request as a list of concrete actions, then a reply.",
    "input_schema": {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": _ACTION_KINDS},
                        "scene_id": {"type": "string",
                                     "description": "Target scene id — a RUNNING scene for edits, an enabled library scene for launch_scene. Omit for 'all running effects'."},
                        "fixture_ids": {"type": "array", "items": {"type": "string"},
                                        "description": "Fixture ids (expand group names to their members yourself)."},
                        "param": {"type": "string"},
                        "value": {"type": "number"},
                        "factor": {"type": "number", "description": "scale_param multiplier, e.g. 0.5 = half speed"},
                        "slot": {"type": "string", "enum": ["primary", "secondary", "accent"]},
                        "color": _color_schema("Emitter levels 0-255 (r g b a w uv l)."),
                        "effect": {"type": "string"},
                        "level": {"type": "number", "minimum": 0, "maximum": 1,
                                  "description": "set_master / set_dimmer: 0.0-1.0"},
                        "mode": {"type": "string", "enum": ["full", "color"]},
                        "on": {"type": "boolean", "description": "singer_mode / freeze / overlay: true = on"},
                        "kelvin": {"type": "integer", "minimum": 1500, "maximum": 10000,
                                   "description": "singer_color: colour temperature"},
                        "palette_id": {"type": "string", "description": "singer_color: id from PALETTE"},
                        "text": {"type": "string", "description": "reply: one short line (≤ 80 chars) for the person who sent the message."},
                        # "scene" (create_scene) is attached per call by _command_tool()
                    },
                    "required": ["kind"],
                },
            },
        },
        "required": ["actions"],
    },
}

_COMMAND_SYSTEM = """You turn short messages from musicians/crew into Lightboard actions during a live show.

Action semantics:
- launch_scene: start a library scene by id (main scenes replace the current look; effects stack).
- stop_scene: fade out one running scene. stop_all_effects / stop_all_main: clear a category.
- blackout: everything dark (toggle-safe; use blackout_off to release). mode 'color' keeps whites.
- blackout_fixtures / restore_fixtures: remove / re-add fixtures from the RUNNING scenes (edit, not stop).
- set_param: set one param on a running effect (registry ranges apply). scale_param: multiply —
  "slower" = factor 0.5, "faster" = 2, "a bit more" = 1.3. Param names come from the registry
  (speed, size, tail, density, brightness, fade, duty…). Omit scene_id to hit every running effect.
- set_color: replace one colour slot on a running effect. "warmer" → add amber/red, cut blue;
  "cooler" → more white/blue; "dimmer" on a colour means lower brightness → prefer set_dimmer.
- swap_effect: change a running effect's type (chase→comet etc.), keeping its colours/fixtures.
- set_master: whole-rig colour dimmer 0-1. set_dimmer: per-fixture dimmer 0-1 on running scenes.
HOW TO BUILD LOOKS (create_scene):
- Timing words → step fields: "5 s step with 1 s crossfade" = fade 1000, hold 4000 (hold is the
  time AFTER the fade). "every 10 s" = fade+hold = 10000.
- A stepped colour chase / colour walk across pods or zones is a MAIN scene whose steps ROTATE a
  pattern — give each fixture `colors` (the palette, ≤4) and a `pattern` of colour indexes; the
  pattern is cycled across the fixture's cells, so you need only as many steps as the pattern has
  entries (3 colours → 3 steps), NOT one step per pod. Step 1 pattern [0,1,2], step 2 [1,2,0],
  step 3 [2,0,1]. "Per pod" = per cell: use a pattern; "per fixture" = one `color` per fixture.
  Single-cell fixtures (pars) are one pod each: to walk the chase across them give each par a
  pattern starting one index later than the previous par (par1 [0], par2 [1], par3 [2], par4 [0]…)
  and rotate those the same way per step.
- Continuous / smooth movement (chase, comet, scanner, wave, colorfade, breathe, rainbow…) is an
  EFFECT scene; `rendering_mode` lock_to_segments = each fixture animates on its own,
  continuous_strip = all enabled fixtures joined into one long strip so the motion travels across them.
- "Red blue purple palette" → colors [{r:255},{b:255},{r:150,b:255,uv:120}] (emitter levels, not
  sRGB). Keep scenes compact: never list a fixture that isn't in the request.
- create_scene: when NOTHING running can be edited to satisfy the request (nothing is playing, or
  the request names fixtures / a look that no running scene covers) and no enabled library scene
  fits, BUILD the scene yourself in `scene` (same format as the operator's scenes: effect scenes
  for anything animated — a "colour fade" between two colours is effect "colorfade", a slow
  "breathe", a "chase"…; main scenes for static looks or stepped crossfades). Pick exactly the
  fixtures asked for; if none are named, use every non-mover fixture. It starts playing at once as
  a TEMPORARY scene. Never refuse a doable request for lack of an existing scene.
- save_scene: write the most recent temporary scene (or scene_id) to the library permanently —
  the person says "save", "keep that", "save it as X" (put X in `text`).
- singer_mode (on/off): the singer-light override — the pods flagged as singer lights (see SYSTEM
  STATE) go to the singer colour on top of whatever is playing. "singer lights off" = singer_mode off.
- singer_level: the singer dimmer, 0.0-1.0 ("100" / "full" = 1.0, "half" = 0.5).
- singer_color: kelvin (warm 2700-3200, neutral 4000, cool 5600+) OR palette_id from PALETTE.
- freeze (on/off): hold the current look; changes queue until unfreeze. overlay (on/off): the
  show's overlay scene.
- undo: revert the previous command.
Live show controls ARE changeable from here (singer, master, blackout, freeze, overlay). Only
patch/config edits (addresses, fixture profiles, network) are not.
- reply: ALWAYS last. ≤ 80 chars, plain, e.g. "✓ bars out, chase at half speed". After a
  create_scene say it is temporary and that "!save" keeps it. If the message is not a lighting
  request or is truly ambiguous, return ONLY a reply saying what you need.

Resolve names loosely: "bars" = fixtures/groups whose names contain bar/EXA; "pars"/"cans" =
par fixtures; "tube" = the pixel strip; scene names fuzzy-match ("UV" → "Full UV").
Never invent scene or fixture ids — only the ones listed."""


def _running_context(engine):
    """Snapshot of what's playing, in a form both the prompt and undo use."""
    with engine._lock:
        mains = [{"id": s["id"], "name": s["name"], "scene": s["scene"]}
                 for s in engine._active_scenes if not s.get("stopping") and s["id"] != getattr(engine, "WASH_TAG", "__wash__")]
        effs  = [{"id": e["id"], "name": e["name"], "scene": e["scene"]}
                 for e in engine._active_effects if e["target"] == 1.0]
        master = engine._master_level
        bo = engine._blackout_mode if engine._blackout_target == 1.0 else None
        singer_mode  = bool(engine._singer_mode)
        singer_level = float(engine._singer_level)
        freeze  = bool(getattr(engine, "_freeze_active", False))
        overlay = getattr(engine, "_overlay_target", 0.0) == 1.0
        ov_name = getattr(engine, "_current_overlay_name", None)
    show = getattr(engine, "_show", {}) or {}
    sc_mode = show.get("singer_color_mode", "custom")
    singer_color = (f"{show.get('singer_kelvin')}K" if sc_mode == "kelvin"
                    else f"palette:{show.get('singer_palette_id')}" if sc_mode == "palette"
                    else "custom")
    return {"mains": mains, "effects": effs, "master": master, "blackout": bo,
            "singer_mode": singer_mode, "singer_level": singer_level, "singer_color": singer_color,
            "freeze": freeze, "overlay": overlay, "overlay_name": ov_name, "palette_ids": []}


def _command_context_text(run, show_cfg, registry, enabled_scenes):
    def brief_effect(e):
        sc = e["scene"]
        p = {k: v for k, v in (sc.get("params") or {}).items()}
        tmp = " TEMPORARY(unsaved)" if str(e["id"]).startswith(TEMP_PREFIX) else ""
        return (f"- id={e['id']}  \"{e['name']}\"{tmp}  effect={sc.get('effect')} params={json.dumps(p)} "
                f"primary={json.dumps(sc.get('primary'))} secondary={json.dumps(sc.get('secondary'))} "
                f"fixtures={sc.get('fixtures_enabled')}")
    def brief_main(m):
        sc = m["scene"]
        fx = list((sc.get("steps") or [{}])[0].get("fixtures", {}).keys())
        tmp = " TEMPORARY(unsaved)" if str(m["id"]).startswith(TEMP_PREFIX) else ""
        return f"- id={m['id']}  \"{m['name']}\"{tmp}  main, {len(sc.get('steps') or [])} step(s), fixtures={fx}"
    fx_lines, grp_lines, singer_fx = [], [], []
    for fx in show_cfg.get("fixtures", []):
        t = fx.get("type") or ("pixel_strip" if fx.get("pixel_count") else "pod")
        sp = fx.get("singer_pods") or []
        fx_lines.append(f"- {fx['id']}  \"{fx.get('name','')}\"  {t}" + (f"  singer_pods={sp}" if sp else ""))
        if sp:
            singer_fx.append(f"{fx.get('name','')} pods {sp}")
    sys_lines = [
        f"singer_mode={'ON' if run.get('singer_mode') else 'OFF'} singer_level={run.get('singer_level', 0):.2f} "
        f"singer_color={run.get('singer_color')}",
        "singer lights: " + ("; ".join(singer_fx) if singer_fx else "(no pods flagged as singer lights)"),
        f"freeze={'ON' if run.get('freeze') else 'off'} overlay={'ON' if run.get('overlay') else 'off'}"
        + (f" ({run.get('overlay_name')})" if run.get('overlay_name') else "")
        + (" — no overlay scene assigned" if not show_cfg.get("overlay_scene") else ""),
        "PALETTE ids: " + ", ".join(run.get("palette_ids") or []),
    ]
    for g in show_cfg.get("groups", []):
        grp_lines.append(f"- \"{g.get('name','')}\" → {g.get('members', [])}")
    lib = [f"- {s['id']}  \"{s['name']}\"  {s.get('scene_type','main')}" for s in enabled_scenes]
    reg = {eid: list(info.get("params", {}).keys()) for eid, info in registry.items()}
    return ("RUNNING NOW:\n"
            + ("\n".join(brief_main(m) for m in run["mains"]) or "(no main scene)") + "\n"
            + ("\n".join(brief_effect(e) for e in run["effects"]) or "(no effects)") + "\n"
            + f"master={run['master']:.2f} blackout={run['blackout']}\n\n"
            "SYSTEM STATE:\n" + "\n".join(sys_lines) + "\n\n"
            "PATCH:\n" + "\n".join(fx_lines) + "\n\nGROUPS:\n" + ("\n".join(grp_lines) or "(none)")
            + "\n\nENABLED LIBRARY SCENES (launchable):\n" + ("\n".join(lib) or "(none)")
            + "\n\nEFFECTS AND THEIR PARAMS:\n" + json.dumps(reg, separators=(",", ":")))


def parse_command(text, sender, run, show_cfg, registry, enabled_scenes, cfg):
    """→ (actions list, usage). One Haiku call; the static tail is cached."""
    c = merged_cfg(cfg)
    system = [_cached(_COMMAND_SYSTEM + "\n\n" + _command_context_text(run, show_cfg, registry, enabled_scenes))]
    user = [{"type": "text", "text": f"From {sender or 'operator'}: {text.strip()}"}]
    fixture_ids = [fx["id"] for fx in show_cfg.get("fixtures", []) if fx.get("type") != "mover"]
    group_ids   = [g["id"] for g in show_cfg.get("groups", []) if g.get("id")]
    tool = _command_tool(registry, fixture_ids, group_ids)
    data, usage = _call_tool(COMMAND_MODEL or c["model"], system, user, tool, max_tokens=8000)
    return data.get("actions") or [], usage


TEMP_PREFIX = "ai_tmp_"


class CommandHistory:
    """Last-command snapshot for undo (one level; enough for a show), plus the
    temporary scenes created from chat (id → scene) until they're saved."""
    def __init__(self):
        self._snap = None
        self._lock = threading.Lock()
        self.temp = {}           # ordered: last inserted = most recent
    def swap(self, snap):
        """Store `snap` as the new undo point, return the previous one."""
        with self._lock:
            prev, self._snap = self._snap, snap
            return prev


def _restore(engine, snap, load_scene):
    """Put the rig back to a _running_context() snapshot."""
    engine.stop_effect_scene(None)
    engine.stop_scene(None)
    for m in snap["mains"]:
        engine.play_scene(dict(m["scene"]), launch_fade_ms=300, scene_id=m["id"], force=True)
    for e in snap["effects"]:
        engine.play_effect_scene(dict(e["scene"]), scene_id=e["id"])
    engine.set_master(snap["master"])
    engine.set_blackout(snap["blackout"] or "off")
    if "singer_mode" in snap:
        engine.set_singer_mode(snap["singer_mode"])
        engine.set_singer_level(snap["singer_level"])


def _apply_effect_edit(engine, run, scene_id, mutate):
    """Run mutate(scene_dict) on one (or every) running effect and hot-swap."""
    hit = 0
    for e in run["effects"]:
        if scene_id and e["id"] != scene_id:
            continue
        sc = json.loads(json.dumps(e["scene"]))
        if mutate(sc) is not False:
            engine.play_effect_scene(sc, scene_id=e["id"])
            e["scene"] = sc              # keep the snapshot current for the next action
            hit += 1
    return hit


def _apply_main_edit(engine, run, scene_id, mutate):
    hit = 0
    for m in run["mains"]:
        if scene_id and m["id"] != scene_id:
            continue
        sc = json.loads(json.dumps(m["scene"]))
        if mutate(sc) is not False:
            engine.refresh_active_scene(m["id"], sc)
            m["scene"] = sc
            hit += 1
    return hit


def execute_actions(actions, engine, show_cfg, registry, load_scene, history, save_scene=None,
                    host=None):
    """Apply parsed actions. Returns {"applied": [...], "reply": str, "errors": [...]}.
    save_scene(scene_dict) → new library id; host = optional dict of app-level
    callbacks: set_singer_color(mode, kelvin, palette_id), set_overlay(on)."""
    host = host or {}
    run = _running_context(engine)
    # The state before THIS command becomes the undo point; the one before
    # the PREVIOUS command is what "undo" restores now.
    prev_snap = history.swap(json.loads(json.dumps(run)))
    fx_ids = {fx["id"] for fx in show_cfg.get("fixtures", [])}
    group_members = {g.get("id"): g.get("members", []) for g in show_cfg.get("groups", [])}
    applied, errors, reply = [], [], None

    def resolve_fx(ids):
        out = []
        for i in ids or []:
            if i in fx_ids:
                out.append(i)
            elif i in group_members:
                out.extend(m for m in group_members[i] if m in fx_ids)
        return list(dict.fromkeys(out))

    for a in actions:
        k = a.get("kind")
        try:
            if k == "reply":
                reply = (a.get("text") or "").strip()[:120]
            elif k == "launch_scene":
                sid = a.get("scene_id")
                sc = load_scene(sid)
                st = sc.get("scene_type", "main")
                if st == "effect":
                    engine.play_effect_scene(sc, scene_id=sid)
                elif st == "main":
                    engine.play_scene(sc, scene_id=sid)
                else:
                    raise ValueError(f"can't launch {st} scenes from chat")
                applied.append(f"launch {sc.get('name')}")
            elif k == "stop_scene":
                sid = a.get("scene_id")
                engine.stop_effect_scene(sid)
                engine.stop_scene(scene_id=sid)
                applied.append(f"stop {sid}")
            elif k == "stop_all_effects":
                engine.stop_effect_scene(None); applied.append("stop all effects")
            elif k == "stop_all_main":
                engine.stop_scene(None); applied.append("stop main scenes")
            elif k == "blackout":
                engine.set_blackout(a.get("mode") or "full"); applied.append("blackout")
            elif k == "blackout_off":
                engine.set_blackout("off"); applied.append("blackout off")
            elif k in ("blackout_fixtures", "restore_fixtures"):
                ids = set(resolve_fx(a.get("fixture_ids")))
                if not ids:
                    raise ValueError("no matching fixtures")
                if k == "blackout_fixtures":
                    def mut_e(sc):
                        sc["fixtures_enabled"] = [f for f in sc.get("fixtures_enabled", []) if f not in ids]
                        sc.setdefault("_ai_removed", []).extend(sorted(ids))
                    def mut_m(sc):
                        for st in sc.get("steps", []):
                            for fid, fd in st.get("fixtures", {}).items():
                                if fid in ids and isinstance(fd, dict):
                                    fd["dimmer"] = 0
                                    for key in ("pods", "segments"):
                                        if key in fd:
                                            fd[key] = [({c: 0 for c in p} if isinstance(p, dict) else p) for p in fd[key]]
                else:
                    def mut_e(sc):
                        rem = [f for f in sc.pop("_ai_removed", []) if f in ids]
                        if not rem: return False
                        sc["fixtures_enabled"] = list(dict.fromkeys(sc.get("fixtures_enabled", []) + rem))
                    def mut_m(sc):
                        # restore from the library copy of the same scene
                        try: orig = load_scene(sc.get("_ai_id"))
                        except Exception: return False
                        for st, ost in zip(sc.get("steps", []), orig.get("steps", [])):
                            for fid in ids:
                                if fid in ost.get("fixtures", {}):
                                    st.setdefault("fixtures", {})[fid] = ost["fixtures"][fid]
                n = _apply_effect_edit(engine, run, a.get("scene_id"), mut_e)
                for m in run["mains"]:
                    m["scene"].setdefault("_ai_id", m["id"])
                n += _apply_main_edit(engine, run, a.get("scene_id"), mut_m)
                applied.append(f"{k.replace('_', ' ')} {sorted(ids)}")
            elif k in ("set_param", "scale_param"):
                pname = a.get("param")
                def mut(sc):
                    spec = registry.get(sc.get("effect"), {}).get("params", {}).get(pname)
                    if not spec: return False
                    cur = sc.setdefault("params", {}).get(pname, spec.get("default", 0))
                    v = float(a.get("value", cur)) if k == "set_param" else float(cur) * float(a.get("factor", 1))
                    v = max(spec.get("min", v), min(spec.get("max", v), v))
                    sc["params"][pname] = int(round(v)) if spec.get("type") == "int" else round(v, 3)
                n = _apply_effect_edit(engine, run, a.get("scene_id"), mut)
                if not n: raise ValueError(f"no running effect has '{pname}'")
                applied.append(f"{pname} {'=' if k=='set_param' else '×'} {a.get('value', a.get('factor'))} on {n} effect(s)")
            elif k == "set_color":
                slot, col = a.get("slot") or "primary", _clamp_color(a.get("color"))
                if not col: raise ValueError("no colour given")
                n = _apply_effect_edit(engine, run, a.get("scene_id"), lambda sc: sc.__setitem__(slot, col))
                if not n: raise ValueError("no running effect to recolour")
                applied.append(f"{slot} → {col}")
            elif k == "swap_effect":
                eid = a.get("effect")
                if eid not in registry: raise ValueError(f"unknown effect {eid}")
                def mut(sc):
                    sc["effect"] = eid
                    spec = registry[eid]["params"]
                    sc["params"] = {p: sc.get("params", {}).get(p, s.get("default")) for p, s in spec.items()}
                n = _apply_effect_edit(engine, run, a.get("scene_id"), mut)
                if not n: raise ValueError("no running effect to swap")
                applied.append(f"effect → {eid}")
            elif k == "set_master":
                engine.set_master(float(a.get("level", 1.0))); applied.append(f"master {a.get('level')}")
            elif k == "set_dimmer":
                lvl = max(0.0, min(1.0, float(a.get("level", 1.0))))
                ids = set(resolve_fx(a.get("fixture_ids")))
                def mut_e(sc):
                    if ids and not (set(sc.get("fixtures_enabled", [])) & ids): return False
                    sc.setdefault("params", {})["brightness"] = round(lvl, 3)
                def mut_m(sc):
                    for st in sc.get("steps", []):
                        for fid, fd in st.get("fixtures", {}).items():
                            if (not ids or fid in ids) and isinstance(fd, dict):
                                fd["dimmer"] = int(round(255 * lvl))
                n = _apply_effect_edit(engine, run, a.get("scene_id"), mut_e) + _apply_main_edit(engine, run, a.get("scene_id"), mut_m)
                applied.append(f"dimmer {lvl:.2f} on {n} scene(s)")
            elif k == "create_scene":
                sc_in = a.get("scene") or {}
                scene, errs2, warns = validate_scene(sc_in, show_cfg, registry, "From chat")
                if errs2: raise ValueError("; ".join(errs2))
                tid = TEMP_PREFIX + uuid.uuid4().hex[:6]
                scene.pop("_warnings", None)
                if scene["scene_type"] == "effect":
                    engine.play_effect_scene(scene, scene_id=tid)
                else:
                    engine.play_scene(scene, scene_id=tid)
                history.temp[tid] = scene
                applied.append(f"created+playing \"{scene['name']}\" ({tid})" + (f" ⚠ {'; '.join(warns)}" if warns else ""))
            elif k == "save_scene":
                if not save_scene: raise ValueError("saving not available")
                tid = a.get("scene_id")
                if not tid or tid not in history.temp:
                    if not history.temp: raise ValueError("no temporary scene to save")
                    tid = list(history.temp)[-1]
                # Save the scene AS IT IS NOW (later "!slower" etc. mutated the
                # running copy), then re-id the running entry to the library id.
                live = _running_context(engine)
                cur = next((e for e in live["effects"] + live["mains"] if e["id"] == tid), None)
                scene = json.loads(json.dumps(cur["scene"] if cur else history.temp[tid]))
                for k in [k for k in scene if k.startswith("_")]:
                    scene.pop(k)
                new_name = (a.get("text") or "").strip()
                if new_name and not new_name.lower().startswith(("save", "keep")):
                    scene["name"] = new_name[:40]
                new_id = save_scene(scene)
                history.temp.pop(tid, None)
                if cur:
                    if scene.get("scene_type") == "effect":
                        engine.play_effect_scene(dict(scene), scene_id=new_id)
                        engine.stop_effect_scene(tid)
                    else:
                        sc2 = dict(scene); sc2["launch_fade"] = 0
                        engine.play_scene(sc2, launch_fade_ms=0, scene_id=new_id, force=True)
                        engine.stop_scene(scene_id=tid)
                applied.append(f"saved \"{scene['name']}\" as {new_id} (Library → From chat)")
            elif k == "singer_mode":
                on = bool(a.get("on", True))
                engine.set_singer_mode(on); applied.append(f"singer {'on' if on else 'off'}")
            elif k == "singer_level":
                lvl = float(a.get("level", 1.0))
                if lvl > 1.0: lvl = lvl / 100.0          # "80" → 0.8
                lvl = max(0.0, min(1.0, lvl))
                engine.set_singer_level(lvl); applied.append(f"singer level {lvl:.2f}")
            elif k == "singer_color":
                fn = host.get("set_singer_color")
                if not fn: raise ValueError("singer colour not available")
                if a.get("palette_id"):
                    fn("palette", None, a["palette_id"]); applied.append(f"singer colour {a['palette_id']}")
                elif a.get("kelvin"):
                    fn("kelvin", int(a["kelvin"]), None); applied.append(f"singer colour {int(a['kelvin'])}K")
                else:
                    raise ValueError("give kelvin or palette_id")
            elif k == "freeze":
                on = bool(a.get("on", True))
                engine.set_freeze(on); applied.append(f"freeze {'on' if on else 'off'}")
            elif k == "overlay":
                fn = host.get("set_overlay")
                if not fn: raise ValueError("overlay not available")
                on = bool(a.get("on", True))
                fn(on); applied.append(f"overlay {'on' if on else 'off'}")
            elif k == "undo":
                if not prev_snap: raise ValueError("nothing to undo")
                _restore(engine, prev_snap, load_scene); applied.append("undo")
                prev_snap = None
            else:
                raise ValueError(f"unknown action {k}")
        except Exception as e:                       # noqa: BLE001
            errors.append(f"{k}: {e}")
            log.warning("command action %s failed: %s", k, e)
    if not reply:
        if applied:
            reply = "✓ " + "; ".join(applied)
        elif not actions:
            reply = "Couldn't turn that into actions — try naming the fixtures and the look more plainly."
        else:
            reply = "Didn't catch a lighting change in that."
    if errors and not applied:
        reply = "✗ " + "; ".join(errors)[:110]
    return {"applied": applied, "errors": errors, "reply": reply}
