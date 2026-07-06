// kiosk_nav.js — kiosk-only "back to show" affordances for admin pages.
//
// Included on every admin page (/, /touch-config, /library, /editor,
// /settings, /wifi). It activates ONLY when the page was loaded via
// localhost — i.e. only inside the Pi's own kiosk Chromium, which install.sh
// points at http://localhost:5000/touch. Browsers on the rig WiFi reach these
// pages via lights.local / an IP, so they never see any of this.
//
// Two affordances:
//   1. A floating "← SHOW" button (always available manual exit).
//   2. A 5-minute idle timer: no touch/key/scroll -> auto-return to /touch,
//      so a screen abandoned in Settings mid-gig finds its own way home.
(function () {
  'use strict';
  var h = window.location.hostname;
  if (h !== 'localhost' && h !== '127.0.0.1') return;

  var IDLE_MS = 5 * 60 * 1000;
  var idleTimer = null;

  function goShow() { window.location.href = '/touch'; }

  function resetIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(goShow, IDLE_MS);
  }

  function init() {
    var btn = document.createElement('button');
    btn.id = 'kiosk-show-btn';
    btn.textContent = '\u2190 SHOW';
    btn.setAttribute('style',
      'position:fixed;bottom:12px;right:12px;z-index:99999;' +
      'padding:10px 16px;min-height:44px;' +
      'background:#13131c;color:#d4d4e8;border:1px solid #e8630a;' +
      'border-radius:8px;font-family:Rajdhani,sans-serif;font-size:14px;' +
      'font-weight:700;letter-spacing:0.05em;cursor:pointer;' +
      'box-shadow:0 2px 10px rgba(0,0,0,0.6);');
    btn.addEventListener('click', goShow);
    document.body.appendChild(btn);

    ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(function (ev) {
      window.addEventListener(ev, resetIdle, { passive: true });
    });
    resetIdle();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
