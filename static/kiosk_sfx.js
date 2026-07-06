// kiosk_sfx.js — kiosk-only audio feedback through the venue screen's
// built-in speakers (52Pi 7" panel; audio path = whatever ALSA default
// sink the Pi routes to — HDMI or the panel's USB audio, see PI_INFRA).
//
// Localhost-gated like kiosk_nav.js / kiosk_osk.js: activates only inside
// the Pi's own kiosk Chromium. Browsers on the rig WiFi (iPad etc.) get
// silent no-ops, so callers never need to guard.
//
// Tones are generated with Web Audio — no sound files. The AudioContext
// is created lazily on the first sound, which always follows a touch,
// so Chromium's autoplay gesture policy is satisfied.
//
// Config: config.json  "kiosk_sfx": {"enabled": true, "volume": 0.5}
// (absent = these defaults). Surfaced to the page as the read-only `sfx`
// key on GET /api/touch/config; touch.html calls kioskSfx.configure(sfx)
// at boot.
//
// API: window.kioskSfx.click()  — short tick: a tap registered
//      window.kioskSfx.ok()     — rising two-note chirp: success
//      window.kioskSfx.err()    — falling low buzz: failure / rejection
//      window.kioskSfx.configure({enabled, volume})
(function () {
  'use strict';

  var noop = function () {};
  window.kioskSfx = { click: noop, ok: noop, err: noop, configure: noop };

  var h = window.location.hostname;
  if (h !== 'localhost' && h !== '127.0.0.1') return;

  var enabled = true;
  var volume  = 0.5;
  var ctx     = null;

  function ac() {
    if (!ctx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
    }
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }

  // One envelope-shaped oscillator note. `when` = seconds from now.
  function tone(freq, dur, type, gain, when) {
    if (!enabled || volume <= 0) return;
    var c = ac();
    if (!c) return;
    var t0 = c.currentTime + (when || 0);
    var o = c.createOscillator();
    var g = c.createGain();
    o.type = type || 'sine';
    o.frequency.setValueAtTime(freq, t0);
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.linearRampToValueAtTime((gain || 1) * volume, t0 + 0.005);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.connect(g);
    g.connect(c.destination);
    o.start(t0);
    o.stop(t0 + dur + 0.02);
  }

  window.kioskSfx = {
    configure: function (cfg) {
      if (!cfg) return;
      if (cfg.enabled !== undefined) enabled = !!cfg.enabled;
      if (cfg.volume !== undefined) {
        var v = parseFloat(cfg.volume);
        if (!isNaN(v)) volume = Math.max(0, Math.min(1, v));
      }
    },
    click: function () {
      tone(1800, 0.03, 'square', 0.15);
    },
    ok: function () {
      tone(880, 0.08, 'sine', 0.5);
      tone(1320, 0.10, 'sine', 0.5, 0.07);
    },
    err: function () {
      tone(220, 0.12, 'square', 0.4);
      tone(180, 0.16, 'square', 0.4, 0.10);
    },
  };
})();
