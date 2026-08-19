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

  function featureAllowed(feats, key) {
    // Absent from the map => allowed (fail-open for untagged/new keys the
    // server didn't send). Explicit false => denied.
    return feats[key] !== false;
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
    const HIDE_GATES = {
      'tag-x': 'annot.tags',            // remove-tag ✕ on each chip
      'tag-ok': 'annot.tags',           // confirm-tag ✓ on each chip
      'region-del': 'annot.boxes',      // delete-box ✕ in the region list
      'region-confirm': 'annot.boxes',  // confirm-box ✓ in the region list
    };
    Object.keys(HIDE_GATES).forEach(cls => {
      const denied = !featureAllowed(feats, HIDE_GATES[cls]);
      scope.querySelectorAll('.' + cls).forEach(el =>
        el.classList.toggle(HIDDEN_CLASS, denied));
    });
    // Inline-editable tag inputs become read-only when tags are locked; the
    // box class-name inputs when boxes are locked.
    const tagsDenied = !featureAllowed(feats, 'annot.tags');
    scope.querySelectorAll('.tag-edit').forEach(el => { el.readOnly = tagsDenied; });
    const boxesDenied = !featureAllowed(feats, 'annot.boxes');
    scope.querySelectorAll('.region-edit').forEach(el => { el.readOnly = boxesDenied; });
    // Annotation edit gates: elements marked data-annot-edit="<key>" become
    // read-only when that key is denied. Inputs/textareas are disabled in place
    // (so the value stays visible); buttons are hidden. Applied to both the
    // controls pane and any dynamically-rendered annotation UI.
    scope.querySelectorAll('[data-annot-edit]').forEach(container => {
      const key = container.getAttribute('data-annot-edit');
      const denied = !featureAllowed(feats, key);
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
    // Force a metadata editor read-only when meta.<type>.edit is denied:
    // disable every input/select/textarea and hide its save button. Safe to
    // call repeatedly (editors re-render on tab switch and file change).
    enforceEditor: function (type) {
      const editKey = 'meta.' + type + '.edit';
      if (this.allowed(editKey)) return;           // editing permitted, leave as-is
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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();