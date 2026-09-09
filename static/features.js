/* Feature/permission enforcement (frontend).
 *
 * Reads the effective feature map from window.CIMAuth.user.features (populated
 * by auth.js from /api/auth/me) and hides any element tagged with a
 * data-feature key the user is not allowed. A denied section key hides the
 * whole section; a denied leaf key hides just that control.
 *
 * This is a UX layer only — the server still enforces the same permissions on
 * the matching endpoints. Hiding here just keeps the UI honest.
 */
(function () {
  const HIDDEN_CLASS = 'cim-feature-hidden';

  function ensureStyle() {
    if (document.getElementById('cim-feature-style')) return;
    const s = document.createElement('style');
    s.id = 'cim-feature-style';
    s.textContent = '.' + HIDDEN_CLASS + '{display:none !important;}';
    document.head.appendChild(s);
  }

  // Permission levels mirror features.py: block(0) < read(1) < write(2).
  // The server sends numeric levels. Absent => fail-open (untagged/new keys).
  function levelOf(feats, key) {
    const v = feats[key];
    if (v === undefined || v === null) return 2;   // fail-open
    if (typeof v === 'boolean') return v ? 2 : 0;  // legacy bool
    if (typeof v === 'number') return v;
    const M = { block: 0, read: 1, write: 2 };
    return (key in M) ? M[key] : (M[v] !== undefined ? M[v] : 2);
  }
  function canRead(feats, key) { return levelOf(feats, key) >= 1; }
  function canWrite(feats, key) { return levelOf(feats, key) >= 2; }

  // Back-compat shim: featureAllowed now means "can read/see". Old ".edit"
  // keys map to a WRITE check on their base feature.
  const EDIT_KEY_BASE = {
    'tab.faces.edit': 'tab.faces', 'tab.albums.edit': 'tab.albums',
    'tab.books.delete': 'tab.books', 'meta.exif.edit': 'meta.exif',
    'meta.iptc.edit': 'meta.iptc', 'meta.xmp.edit': 'meta.xmp',
  };
  function featureAllowed(feats, key) {
    if (key in EDIT_KEY_BASE) return canWrite(feats, EDIT_KEY_BASE[key]);
    return canRead(feats, key);
  }

  function apply(root) {
    const feats = (window.CIMAuth && window.CIMAuth.user &&
                   window.CIMAuth.user.features) || {};
    ensureStyle();
    const scope = root || document;
    scope.querySelectorAll('[data-feature]').forEach(el => {
      const key = el.getAttribute('data-feature');
      el.classList.toggle(HIDDEN_CLASS, !featureAllowed(feats, key));
    });
    const EDIT_GATES = { 'faces-edit-only': 'tab.faces.edit' };
    Object.keys(EDIT_GATES).forEach(cls => {
      const allowed = featureAllowed(feats, EDIT_GATES[cls]);
      scope.querySelectorAll('.' + cls).forEach(el =>
        el.classList.toggle(HIDDEN_CLASS, !allowed));
    });
    // Class-based edit gates for dynamically-rendered controls. Buttons/spans
    // in this list are hidden when the mapped key is denied; inputs/textareas
    // with these classes are made read-only instead (handled below).
    // Class-based EDIT gates: these controls modify data, so they require
    // WRITE on the mapped key (read = can see, write = can change).
    const HIDE_GATES = {
      'tag-x': 'annot.tags',
      'tag-ok': 'annot.tags',
      'region-del': 'annot.boxes',
      'region-confirm': 'annot.boxes',
    };
    Object.keys(HIDE_GATES).forEach(cls => {
      const denied = !canWrite(feats, HIDE_GATES[cls]);
      scope.querySelectorAll('.' + cls).forEach(el =>
        el.classList.toggle(HIDDEN_CLASS, denied));
    });
    const tagsDenied = !canWrite(feats, 'annot.tags');
    scope.querySelectorAll('.tag-edit').forEach(el => { el.readOnly = tagsDenied; });
    const boxesDenied = !canWrite(feats, 'annot.boxes');
    scope.querySelectorAll('.region-edit').forEach(el => { el.readOnly = boxesDenied; });
    // Annotation edit gates: elements marked data-annot-edit="<key>" become
    // read-only when that key is denied. Inputs/textareas are disabled in place
    // (so the value stays visible); buttons are hidden. Applied to both the
    // controls pane and any dynamically-rendered annotation UI.
    scope.querySelectorAll('[data-annot-edit]').forEach(container => {
      const key = container.getAttribute('data-annot-edit');
      const denied = !canWrite(feats, key);   // editing => write level
      const gate = el => {
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
          el.readOnly = denied;
        } else if (el.tagName === 'SELECT') {
          el.disabled = denied;
        } else if (el.tagName === 'BUTTON') {
          el.classList.toggle(HIDDEN_CLASS, denied);
        }
      };
      if (container.matches('input, textarea, select, button')) gate(container);
      container.querySelectorAll('input, textarea, select, button').forEach(gate);
    });
  }

  // Public entry point: call after auth is ready and after any dynamic
  // markup that carries data-feature is inserted.
  window.CIMFeatures = {
    apply: apply,
    allowed: function (key) {
      const feats = (window.CIMAuth && window.CIMAuth.user &&
                     window.CIMAuth.user.features) || {};
      return featureAllowed(feats, key);
    },
    // Level-aware helpers modules can use.
    level: function (key) {
      const feats = (window.CIMAuth && window.CIMAuth.user &&
                     window.CIMAuth.user.features) || {};
      return levelOf(feats, key);
    },
    canRead: function (key) {
      const feats = (window.CIMAuth && window.CIMAuth.user &&
                     window.CIMAuth.user.features) || {};
      return canRead(feats, key);
    },
    canWrite: function (key) {
      const feats = (window.CIMAuth && window.CIMAuth.user &&
                     window.CIMAuth.user.features) || {};
      return canWrite(feats, key);
    },
    // Force a metadata editor read-only when the user lacks WRITE on the tab.
    enforceEditor: function (type) {
      const key = 'meta.' + type;                  // write on the base feature
      if (this.canWrite(key)) return;              // editing permitted
      const rootId = type + '-editor';             // exif-editor / iptc-editor / xmp-editor
      const applyOnce = () => {
        const root = document.getElementById(rootId);
        if (!root) return false;
        const fields = root.querySelectorAll('input, select, textarea');
        fields.forEach(el => {
          if (el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'radio') {
            el.disabled = true;
          } else {
            el.readOnly = true;
          }
        });
        const save = document.getElementById(type + '-save');
        if (save) save.classList.add(HIDDEN_CLASS);
        return fields.length > 0;
      };
      // Editors render asynchronously (load() fetches then builds the DOM), so
      // retry a few times until the fields exist rather than firing once and
      // possibly missing them. Harmless if it re-runs after fields appear.
      let tries = 0;
      const tick = () => {
        const done = applyOnce();
        if (!done && ++tries < 20) setTimeout(tick, 50);
      };
      tick();
    }
  };

  function run() {
    const ready = (window.CIMAuth && window.CIMAuth.ready) ||
                  Promise.resolve();
    ready.then(() => apply(document)).catch(() => apply(document));
  }

  // Let dynamically-injected markup (e.g. module buttons that carry
  // data-feature) re-run the visibility pass after insertion.
  window.applyFeatureVisibility = function(root){ apply(root || document); };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();