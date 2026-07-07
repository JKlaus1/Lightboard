// kiosk_osk.js — on-screen keyboard for the Pi's kiosk browser.
//
// Loaded by kiosk_nav.js (localhost only), so it never appears on iPads or
// laptops, which have real keyboards. Attaches at the document level via
// focusin, so inputs added dynamically (modals, editors) work automatically.
//
// Layouts: QWERTY (+shift one-shot, +symbols layer) for text-like inputs,
// and a compact numeric pad for type="number". Chromium quirk handled:
// number/email inputs expose no selection APIs, so they get append/trim-end
// editing instead of cursor-position insertion.
(function () {
  'use strict';
  var h = window.location.hostname;
  if (h !== 'localhost' && h !== '127.0.0.1') return;

  var SELECTOR = 'input[type="text"], input[type="password"], input[type="search"], ' +
                 'input[type="url"], input[type="tel"], input[type="email"], ' +
                 'input[type="number"], input:not([type]), textarea';

  var kb = null;          // container element
  var curInput = null;    // focused input being served
  var shift = false;      // one-shot shift
  var layer = 'text';     // 'text' | 'sym' | 'num'
  var hideTimer = null;

  var ROWS = {
    text: [
      ['q','w','e','r','t','y','u','i','o','p'],
      ['a','s','d','f','g','h','j','k','l'],
      ['SHIFT','z','x','c','v','b','n','m','BKSP'],
      ['123',',','SPACE','.','DONE'],
    ],
    sym: [
      ['1','2','3','4','5','6','7','8','9','0'],
      ['-','_','/',':',';','(',')','&','@'],
      ['#','%','+','=','?','!','\'','"','BKSP'],
      ['ABC',',','SPACE','.','DONE'],
    ],
    num: [
      ['7','8','9'],
      ['4','5','6'],
      ['1','2','3'],
      ['-','0','.'],
      ['BKSP','DONE'],
    ],
  };

  function hasSelectionApi(el) {
    try { return el.selectionStart !== null; } catch (e) { return false; }
  }

  function fireInput(el) {
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function insertText(el, str) {
    if (hasSelectionApi(el)) {
      var s = el.selectionStart, e = el.selectionEnd;
      el.setRangeText(str, s, e, 'end');
    } else {
      el.value = el.value + str;   // number/email: append-at-end mode
    }
    fireInput(el);
  }

  function backspace(el) {
    if (hasSelectionApi(el)) {
      var s = el.selectionStart, e = el.selectionEnd;
      if (s === e && s > 0) { s = s - 1; }
      el.setRangeText('', s, e, 'end');
    } else {
      el.value = el.value.slice(0, -1);
    }
    fireInput(el);
  }

  function keyLabel(k) {
    if (k === 'SHIFT') return shift ? '\u21e7\u25cf' : '\u21e7';
    if (k === 'BKSP')  return '\u232b';
    if (k === 'SPACE') return ' ';
    if (k === 'DONE')  return 'done';
    if (k.length === 1 && shift && layer === 'text') return k.toUpperCase();
    return k;
  }

  function pressKey(k) {
    if (!curInput) return;
    if (k === 'SHIFT') { shift = !shift; render(); return; }
    if (k === 'BKSP')  { backspace(curInput); return; }
    if (k === 'DONE')  {
      var el = curInput;
      var koskRect = kb ? kb.getBoundingClientRect() : null;
      hideKb();
      el.blur();
      swallowFallThroughClick(koskRect);
      return;
    }
    if (k === '123')   { layer = 'sym';  render(); return; }
    if (k === 'ABC')   { layer = 'text'; render(); return; }
    var ch = (k === 'SPACE') ? ' ' : k;
    if (ch.length === 1 && shift && layer === 'text') { ch = ch.toUpperCase(); shift = false; render(); }
    insertText(curInput, ch);
  }

  function render() {
    if (!kb) return;
    kb.innerHTML = '';
    ROWS[layer].forEach(function (row) {
      var r = document.createElement('div');
      r.className = 'kosk-row';
      row.forEach(function (k) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'kosk-key';
        if (k === 'SPACE') b.classList.add('kosk-space');
        if (k === 'DONE')  b.classList.add('kosk-done');
        if (k === 'SHIFT' || k === 'BKSP' || k === '123' || k === 'ABC') b.classList.add('kosk-fn');
        b.textContent = keyLabel(k);
        // pointerdown + preventDefault: the key press never steals focus
        // from the input, so typing continues seamlessly.
        b.addEventListener('pointerdown', function (e) {
          e.preventDefault();
          pressKey(k);
        });
        r.appendChild(b);
      });
      kb.appendChild(r);
    });
  }

  function buildKb() {
    if (kb) return;
    var css = document.createElement('style');
    css.textContent =
      '#kosk{position:fixed;left:0;right:0;bottom:0;z-index:999999;' +
        'background:#0d0d14;border-top:1px solid #1e1e2e;' +
        'padding:6px 4px calc(6px + env(safe-area-inset-bottom));' +
        'display:none;user-select:none;-webkit-user-select:none;' +
        'touch-action:manipulation;}' +
      '#kosk.show{display:block;}' +
      '.kosk-row{display:flex;gap:5px;justify-content:center;margin-bottom:5px;}' +
      '.kosk-key{flex:1;max-width:92px;min-height:48px;' +
        'background:#1a1a24;border:1px solid #2a2a3a;border-radius:7px;' +
        'color:#d4d4e8;font-family:Rajdhani,sans-serif;font-size:19px;' +
        'font-weight:700;cursor:pointer;}' +
      '.kosk-key:active{background:#2c2c40;}' +
      '.kosk-fn{max-width:120px;font-size:15px;color:#8888a8;}' +
      '.kosk-space{flex:4;max-width:none;}' +
      '.kosk-done{background:#e8630a;border-color:#e8630a;color:#000;font-size:15px;}';
    document.head.appendChild(css);
    kb = document.createElement('div');
    kb.id = 'kosk';
    document.body.appendChild(kb);
  }

  function showKb(input) {
    buildKb();
    curInput = input;
    layer = (input.type === 'number') ? 'num' : 'text';
    shift = false;
    render();
    kb.classList.add('show');
    document.body.style.paddingBottom = kb.offsetHeight + 'px';
    setTimeout(function () {
      try { input.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (e) {}
    }, 50);
  }

  function hideKb() {
    if (!kb) return;
    kb.classList.remove('show');
    document.body.style.paddingBottom = '';
    curInput = null;
  }

  // After DONE dismisses the keyboard, the tap's trailing click would fall
  // through onto whatever control sat beneath it (e.g. the editor's fixed
  // Save bar, which shares the bottom edge). Eat exactly one click that lands
  // in the keyboard's former band within a short window; taps elsewhere or
  // later pass through untouched.
  function swallowFallThroughClick(rect) {
    if (!rect) return;
    var cleanup = function () {
      document.removeEventListener('click', swallow, true);
      clearTimeout(t);
    };
    var swallow = function (ev) {
      var inBand = ev.clientY >= rect.top;
      cleanup();
      if (inBand) { ev.preventDefault(); ev.stopPropagation(); }
    };
    var t = setTimeout(cleanup, 400);
    document.addEventListener('click', swallow, true);
  }

  document.addEventListener('focusin', function (e) {
    if (!e.target.matches || !e.target.matches(SELECTOR)) return;
    if (e.target.readOnly || e.target.disabled) return;
    clearTimeout(hideTimer);
    showKb(e.target);
  });

  document.addEventListener('focusout', function (e) {
    if (e.target !== curInput) return;
    // small delay: a focusin on another eligible input cancels the hide,
    // and key presses never blur (pointerdown is prevented)
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideKb, 150);
  });
})();
