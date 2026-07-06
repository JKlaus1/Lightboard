// dialogs.js — in-page async form dialogs (replacement for native prompt()).
//
// Why: native prompt() is a browser-chrome modal, so the kiosk on-screen
// keyboard (kiosk_osk.js, which hooks focusin on DOM inputs) can never
// attach to it. These dialogs are real DOM inputs, so the OSK appears
// automatically on the kiosk — and they look better on the iPad too.
//
// Usage:
//   const v = await formDialog({
//     title: 'Name this preset',
//     hint:  'Saves the current live look',        // optional
//     ok:    'Save',                                // optional, default 'OK'
//     fields: [
//       { key:'name', label:'Name', value:'', placeholder:'Preset' },
//       { key:'lo', label:'Low', type:'number', min:0, max:255, value:0 },
//     ],
//   });
//   if (v === null) return;   // cancelled (tap-off, Cancel, or Esc)
//   // else v = { name:'...', lo:0, ... }  (numbers clamped to min/max)
//
// Top-anchored so the bottom-fixed OSK never covers the fields.
// z-index sits above page UI and the kiosk nav pill (99999) but below
// the OSK itself (999999).
(function () {
  'use strict';

  var CSS =
    '.dlg-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.6);' +
      'z-index:200000;display:flex;justify-content:center;' +
      'align-items:flex-start;padding-top:7vh;}' +
    '.dlg-panel{background:var(--surface,#14141f);color:var(--text,#e8e8f0);' +
      'border:1px solid var(--border,#2a2a3a);border-radius:12px;' +
      'padding:1rem 1.1rem;width:min(92vw,420px);' +
      'box-shadow:0 8px 32px rgba(0,0,0,0.5);' +
      'font-family:inherit;}' +
    '.dlg-panel h3{margin:0 0 0.25rem;font-size:1.05rem;}' +
    '.dlg-hint{margin:0 0 0.6rem;font-size:0.78rem;opacity:0.65;}' +
    '.dlg-field{margin-bottom:0.7rem;}' +
    '.dlg-field label{display:block;font-size:0.78rem;opacity:0.8;' +
      'margin-bottom:0.25rem;}' +
    '.dlg-field input{width:100%;box-sizing:border-box;font-size:1.05rem;' +
      'padding:0.55rem 0.6rem;border-radius:8px;' +
      'border:1px solid var(--border,#2a2a3a);' +
      'background:var(--surface2,#1c1c2b);color:inherit;}' +
    '.dlg-btns{display:flex;gap:0.6rem;margin-top:0.9rem;}' +
    '.dlg-btns button{flex:1;font-size:1rem;padding:0.6rem 0;' +
      'border-radius:8px;border:1px solid var(--border,#2a2a3a);' +
      'cursor:pointer;}' +
    '.dlg-cancel{background:var(--surface2,#1c1c2b);color:inherit;}' +
    '.dlg-ok{background:var(--accent,#4a6cf7);border-color:transparent;' +
      'color:#fff;font-weight:600;}';

  function ensureCss() {
    if (document.getElementById('dlg-css')) return;
    var s = document.createElement('style');
    s.id = 'dlg-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  window.formDialog = function (opts) {
    return new Promise(function (resolve) {
      ensureCss();
      var fields = opts.fields || [];

      var back = document.createElement('div');
      back.className = 'dlg-backdrop';
      var panel = document.createElement('div');
      panel.className = 'dlg-panel';
      back.appendChild(panel);

      var h = document.createElement('h3');
      h.textContent = opts.title || '';
      panel.appendChild(h);
      if (opts.hint) {
        var p = document.createElement('p');
        p.className = 'dlg-hint';
        p.textContent = opts.hint;
        panel.appendChild(p);
      }

      fields.forEach(function (f) {
        var wrap = document.createElement('div');
        wrap.className = 'dlg-field';
        if (f.label) {
          var lab = document.createElement('label');
          lab.textContent = f.label;
          wrap.appendChild(lab);
        }
        var inp = document.createElement('input');
        inp.type = f.type || 'text';
        if (f.type === 'number') {
          if (f.min !== undefined) inp.min = f.min;
          if (f.max !== undefined) inp.max = f.max;
          inp.inputMode = 'numeric';
        }
        inp.value = (f.value !== undefined && f.value !== null) ? f.value : '';
        if (f.placeholder) inp.placeholder = f.placeholder;
        inp.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') { e.preventDefault(); submit(); }
        });
        f._el = inp;
        wrap.appendChild(inp);
        panel.appendChild(wrap);
      });

      var btns = document.createElement('div');
      btns.className = 'dlg-btns';
      var bCancel = document.createElement('button');
      bCancel.type = 'button';
      bCancel.className = 'dlg-cancel';
      bCancel.textContent = 'Cancel';
      bCancel.onclick = function () { done(null); };
      var bOk = document.createElement('button');
      bOk.type = 'button';
      bOk.className = 'dlg-ok';
      bOk.textContent = opts.ok || 'OK';
      bOk.onclick = submit;
      btns.appendChild(bCancel);
      btns.appendChild(bOk);
      panel.appendChild(btns);

      function onKey(e) { if (e.key === 'Escape') done(null); }

      function done(val) {
        document.removeEventListener('keydown', onKey);
        back.remove();
        resolve(val);
      }

      function submit() {
        var out = {};
        for (var i = 0; i < fields.length; i++) {
          var f = fields[i];
          var v = f._el.value;
          if (f.type === 'number') {
            v = parseInt(v, 10);
            if (isNaN(v)) v = (f.min !== undefined ? f.min : 0);
            if (f.min !== undefined && v < f.min) v = f.min;
            if (f.max !== undefined && v > f.max) v = f.max;
          } else {
            v = String(v).trim();
          }
          out[f.key] = v;
        }
        done(out);
      }

      back.addEventListener('click', function (e) {
        if (e.target === back) done(null);
      });
      document.addEventListener('keydown', onKey);
      document.body.appendChild(back);
      if (fields.length && fields[0]._el) fields[0]._el.focus();
    });
  };
})();
