"""Lightboard AI routes (/api/ai/*).

Registered from app.py via register_ai_routes(app, ctx). `ctx` is a dict of
callables/objects from app.py so this module never imports app:

    ctx = {
      "config":            lambda: config,            # effective config dict
      "save_config":       save_config,
      "show_config":       lambda: show_config,       # active show dict
      "effects_registry":  effects.get_registry,
      "list_library_scenes": list_library_scenes,
      "load_library_scene":  load_library_scene,
      "engine":            engine,
    }

All generation runs in ai.Jobs threads; the UI polls /api/ai/job/<id>.
Results are proposals only — saving goes through the normal
/api/fixtures and /api/library/scene routes from the page.
"""

import logging
from flask import request, jsonify

import ai

log = logging.getLogger(__name__)
JOBS = ai.Jobs()


def register_ai_routes(app, ctx):

    def _cfg():
        return ai.merged_cfg(ctx["config"]().get("ai"))

    def _gate(feature=None):
        st = ai.status(ctx["config"]().get("ai"))
        if not st["sdk"]:
            return "anthropic SDK not installed — run: .venv/bin/pip install -r requirements.txt"
        if not st["key"]["configured"]:
            return "No API key — add one in Settings → AI."
        if not st["cfg"]["enabled"]:
            return "AI features are switched off in Settings → AI."
        if feature and not st["cfg"].get(feature, True):
            return f"'{feature}' is switched off in Settings → AI."
        return None

    # ── status / config / key ─────────────────────────────────────────
    @app.route("/api/ai/status")
    def api_ai_status():
        return jsonify(ai.status(ctx["config"]().get("ai")))

    @app.route("/api/ai/config", methods=["POST"])
    def api_ai_config():
        data = request.json or {}
        cfg  = ctx["config"]()
        cur  = ai.merged_cfg(cfg.get("ai"))
        for k in ai.DEFAULT_CFG:
            if k in data:
                v = data[k]
                if k == "messenger_allow":
                    v = [str(x).strip() for x in (v or []) if str(x).strip()]
                elif k == "model":
                    v = str(v).strip() or ai.DEFAULT_MODEL
                else:
                    v = bool(v)
                cur[k] = v
        cfg["ai"] = cur
        ctx["save_config"]()
        return jsonify({"ok": True, "cfg": cur})

    @app.route("/api/ai/key", methods=["POST"])
    def api_ai_key_set():
        key = (request.json or {}).get("key", "")
        if key and not key.startswith("sk-ant-"):
            return jsonify({"ok": False, "error": "That doesn't look like a Claude API key (sk-ant-…)."}), 400
        if ai.get_key()[1] == "env" and key:
            return jsonify({"ok": False, "error": "Key is set by ANTHROPIC_API_KEY in the environment; "
                                                  "the file would be ignored."}), 400
        ai.set_key(key)
        return jsonify({"ok": True, "key": ai.key_status()})

    @app.route("/api/ai/key", methods=["DELETE"])
    def api_ai_key_clear():
        ai.set_key("")
        return jsonify({"ok": True, "key": ai.key_status()})

    @app.route("/api/ai/test", methods=["POST"])
    def api_ai_test():
        err = _gate()
        if err:
            return jsonify({"ok": False, "error": err}), 400
        try:
            return jsonify(ai.test_connection(_cfg()))
        except Exception as e:                      # noqa: BLE001
            return jsonify({"ok": False, "error": ai._friendly_error(e)}), 502

    # ── jobs ──────────────────────────────────────────────────────────
    @app.route("/api/ai/job/<jid>")
    def api_ai_job(jid):
        j = JOBS.get(jid)
        if not j:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        j["ok"] = True
        return jsonify(j)

    # ── Feature 1: fixture from manual ────────────────────────────────
    @app.route("/api/ai/fixture-from-manual", methods=["POST"])
    def api_ai_fixture_from_manual():
        err = _gate("fixture_from_manual")
        if err:
            return jsonify({"ok": False, "error": err}), 400
        f = request.files.get("pdf")
        if not f or not f.filename:
            return jsonify({"ok": False, "error": "Attach a PDF manual as 'pdf'."}), 400
        if not f.filename.lower().endswith(".pdf") and (f.mimetype or "") != "application/pdf":
            return jsonify({"ok": False, "error": "Only PDF manuals are supported."}), 400
        data = f.read()
        if len(data) > ai.MAX_PDF_BYTES:
            return jsonify({"ok": False, "error": "PDF larger than 30 MB."}), 400
        hint = (request.form.get("hint") or "").strip()[:500]
        jid  = JOBS.start("fixture", ai.fixture_from_manual, data, f.filename, hint, _cfg())
        return jsonify({"ok": True, "job": jid})

    # ── Feature 2: scenes from prompt / setlist ───────────────────────
    def _scene_ctx():
        show = ctx["show_config"]()
        reg  = ctx["effects_registry"]()
        ex   = ai.pick_examples(ctx["list_library_scenes"](), ctx["load_library_scene"])
        return show, reg, ex

    @app.route("/api/ai/scenes-from-prompt", methods=["POST"])
    def api_ai_scenes_from_prompt():
        err = _gate("scene_from_prompt")
        if err:
            return jsonify({"ok": False, "error": err}), 400
        d = request.json or {}
        prompt = (d.get("prompt") or "").strip()
        if len(prompt) < 3:
            return jsonify({"ok": False, "error": "Describe the scene first."}), 400
        n  = d.get("n", 3)
        st = d.get("scene_type") or "auto"
        if st not in ("auto", "effect", "main"):
            st = "auto"
        show, reg, ex = _scene_ctx()
        jid = JOBS.start("scenes", ai.scenes_from_prompt, prompt[:2000], n, st,
                         show, reg, ex, _cfg(), d.get("folder") or "AI drafts")
        return jsonify({"ok": True, "job": jid})

    @app.route("/api/ai/scenes-from-setlist", methods=["POST"])
    def api_ai_scenes_from_setlist():
        err = _gate("scene_from_prompt")
        if err:
            return jsonify({"ok": False, "error": err}), 400
        d = request.json or {}
        setlist = (d.get("setlist") or "")[:6000]
        lines = [ln for ln in setlist.splitlines() if ln.strip()]
        if not lines:
            return jsonify({"ok": False, "error": "Setlist is empty."}), 400
        if len(lines) > 30:
            return jsonify({"ok": False, "error": "Setlist limited to 30 songs per run."}), 400
        show, reg, ex = _scene_ctx()
        jid = JOBS.start("setlist", ai.scenes_from_setlist,
                         setlist, (d.get("style") or "")[:500],
                         show, reg, ex, _cfg(), d.get("folder") or "Setlist")
        return jsonify({"ok": True, "job": jid})

    # ── preview helper (lets the AI panel audition a candidate) ───────
    @app.route("/api/ai/preview", methods=["POST"])
    def api_ai_preview():
        """Body {"scene": {...}} → live preview via the engine's own preview
        paths; {"clear": true} → stop. Same calls the editors make."""
        d = request.json or {}
        eng = ctx["engine"]
        if d.get("clear"):
            try: eng.preview_effect_clear()
            except Exception: pass
            try: eng.preview_clear("main")
            except Exception: pass
            return jsonify({"ok": True})
        sc = d.get("scene") or {}
        st = sc.get("scene_type")
        try:
            if st == "effect":
                eng.preview_clear("main")
                eng.preview_effect(sc)
            elif st == "main":
                eng.preview_effect_clear()
                step = (sc.get("steps") or [{}])[0]
                eng.preview_set("main", step.get("fixtures") or {})
            else:
                return jsonify({"ok": False, "error": "unsupported scene_type"}), 400
        except Exception as e:                      # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    log.info("AI routes registered (%s)", "ready" if _gate() is None else _gate())
