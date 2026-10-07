/* user_settings.js - Settings -> User settings.
 *
 * Per-account settings declared with host.add_user_setting (core: search
 * quick-filters; theming: layout, palette; any module can add more). Every
 * signed-in user can save their own - no settings.* permission involved; a
 * setting that names a feature (theme.choose) is read-only without WRITE on it,
 * and the server enforces the same.
 *
 * Rendered with the same widgets as module settings (moduleFieldEl), edits are
 * buffered and written by the modal's Save; "Reset" puts a setting back to its
 * default (the admin's, or the role's). After a save, "cim:user-settings" fires
 * on window with {keys} so the app can react (theming reloads the theme, the
 * search chips refresh).
 */
(function () {
  "use strict";

  let pending = {};

  async function loadUserSettings(force) {
    if (window._userSettingsLoaded && !force) return;
    const mount = document.getElementById("user_settings_fields");
    if (!mount) return;
    let fields = [];
    try {
      const d = await fetch("/api/user/settings").then(r => r.json());
      fields = (d && d.success && d.fields) || [];
    } catch (e) {
      mount.innerHTML = '<p class="text-xs text-red-400">Could not load your settings.</p>';
      return;
    }
    pending = {};
    render(mount, fields);
    window._userSettingsLoaded = true;
  }

  function render(mount, fields) {
    mount.innerHTML = "";
    if (!fields.length) {
      mount.innerHTML = '<p class="text-xs text-gray-500">Nothing to set here yet.</p>';
      return;
    }
    for (const f of fields) {
      const row = document.createElement("div");
      row.className = "user-setting";
      row.dataset.key = f.key;
      const el = window.moduleFieldEl
        ? window.moduleFieldEl(f, (k, v) => { pending[k] = v; markDirty(); })
        : document.createTextNode(f.label);
      row.appendChild(el);
      const foot = document.createElement("div");
      foot.className = "flex items-center gap-2 mt-1 text-[10px] text-gray-500";
      if (!f.editable) {
        foot.innerHTML = "<span>Set by your administrator.</span>";
        el.querySelectorAll && el.querySelectorAll("input, select, textarea").forEach(i => {
          if (i.tagName === "SELECT" || i.type === "checkbox") i.disabled = true; else i.readOnly = true;
        });
        el.querySelectorAll && el.querySelectorAll("button").forEach(b => b.classList.add("hidden"));
      } else {
        foot.innerHTML = `<span>${f.is_set ? "Your own setting." : "Using the default."}</span>`;
        if (f.is_set) {
          const reset = document.createElement("button");
          reset.type = "button"; reset.textContent = "Reset to default";
          reset.className = "text-sky-400 hover:text-sky-300";
          reset.addEventListener("click", () => {
            pending[f.key] = null; markDirty();
            foot.innerHTML = "<span>Will use the default after Save.</span>";
          });
          foot.appendChild(reset);
        }
      }
      row.appendChild(foot);
      mount.appendChild(row);
    }
  }

  function markDirty() {
    const b = document.getElementById("user_settings_dirty");
    if (b) b.textContent = Object.keys(pending).length ? "Unsaved changes - click Save below." : "";
  }

  async function persistUserSettings() {
    const keys = Object.keys(pending);
    if (!keys.length) return { ok: true };
    try {
      const r = await fetch("/api/user/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pending),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.success) return { ok: false, error: d.error || "Your settings failed to save" };
    } catch (e) { return { ok: false, error: "Your settings failed to save" }; }
    pending = {};
    window._userSettingsLoaded = false;
    window.dispatchEvent(new CustomEvent("cim:user-settings", { detail: { keys } }));
    return { ok: true };
  }

  window.loadUserSettings = loadUserSettings;
  window.persistUserSettings = persistUserSettings;
  // No tab: always runs (it is the user's own data), no-op when untouched.
  if (window.registerSettingsPersist) window.registerSettingsPersist(persistUserSettings);
  else (window._settingsPersistSteps = window._settingsPersistSteps || []).push({ fn: persistUserSettings, tab: null });
})();