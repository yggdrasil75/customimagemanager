"""
Theming — the core theme registry and per-user theme selection.

A theme has one of two kinds:

  functional   WHAT the interface shows and how it is laid out (which panes,
               how a picture opens, how big things are). Exactly one is active.
  colorings    WHAT COLOUR it is. Exactly one is active.

This module owns the mechanism only; the themes themselves are modules that
register with the `theming` service (see modules/README.md, "Themes"). The
front end (theming.js) sets body[data-functional="<id>"] and
body[data-colorings="<id>"]; a theme's CSS/JS keys off those attributes.

Permissions. A functional theme is presentation: it may hide controls a user
is allowed to use, never show ones they are not. The permission layer
(features.js on the client, require_feature on every route) is unaffected by
which theme is active, and theming.js re-applies feature visibility after
every switch. Picking one's own theme is itself a feature, `theme.choose`;
a user without it gets the server defaults.

Routes
  GET  /api/theme   -> {themes: {functional: [...], colorings: [...]},
                        selected: {functional, colorings}, chosen: {...},
                        defaults: {...}, can_choose}
  POST /api/theme   {functional?: id, colorings?: id}  (theme.choose, write)
                     an empty string clears the user's pick (-> default)
"""

from flask import g, jsonify, request

MANIFEST = {
    "id":          "theming",
    "name":        "Theming",
    "version":     "1.0.0",
    "description": "Theme registry (functional + colorings) and per-user theme choice. "
                   "Themes are separate modules.",
    "core":        True,
    "requires":    [],
    "pip":         [],
    "assets":      ["theming.js", "theming.css"],
}

KINDS = ("functional", "colorings")
_ID_OK = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


class ThemeRegistry:
    """What theme modules register into; published as the `theming` service."""

    def __init__(self, host):
        self._host = host
        self._themes = {k: {} for k in KINDS}     # kind -> id -> spec

    def register(self, kind, theme_id, label, *, description="", default=False):
        """Declare a theme. kind is "functional" or "colorings"; theme_id is the
        value the body attribute takes (lowercase letters, digits, _ -). default=True
        offers this theme as the fallback when the admin set none."""
        if kind not in KINDS:
            raise ValueError(f"theme kind must be one of {KINDS}")
        theme_id = str(theme_id or "").strip().lower()
        if not theme_id or set(theme_id) - _ID_OK:
            raise ValueError("theme id: lowercase letters, digits, _ and - only")
        self._themes[kind][theme_id] = {
            "id": theme_id, "label": label or theme_id, "description": description or "",
            "default": bool(default), "module_id": self._host._current_module,
        }
        return theme_id

    def themes(self, kind=None):
        if kind:
            return list(self._themes[kind].values())
        return {k: list(v.values()) for k, v in self._themes.items()}

    def has(self, kind, theme_id):
        return theme_id in self._themes.get(kind, {})

    def fallback(self, kind):
        """The theme a kind falls back to: the one flagged default, else the first
        registered, else '' (no theme -> the body attribute is left unset)."""
        ts = list(self._themes[kind].values())
        for t in ts:
            if t["default"]:
                return t["id"]
        return ts[0]["id"] if ts else ""


def register(host):
    reg = ThemeRegistry(host)
    host.provide_service("theming", reg)

    host.add_table("""
        CREATE TABLE IF NOT EXISTS user_prefs (
            username TEXT NOT NULL,
            key      TEXT NOT NULL,
            value    TEXT,
            PRIMARY KEY (username, key)
        )""")

    host.register_feature("theme.choose", "Theme: pick own interface / colours (write=change)",
                          section="settings", section_label="Settings", default="write",
                          role_defaults={"viewer": "write", "uploader": "write"})

    # Admin-set defaults: what a user who hasn't picked (or may not pick) gets.
    def _opts(kind):
        def f():
            return [{"value": "", "label": "(module default)"}] + \
                   [{"value": t["id"], "label": t["label"]} for t in reg.themes(kind)]
        return f
    for kind, label in (("functional", "Default interface (functional theme)"),
                        ("colorings", "Default colours (colorings theme)")):
        key = f"theme_default_{kind}"
        host.add_config_key(key, default="",
                            validate=lambda v, k=kind: v if (v == "" or reg.has(k, str(v))) else "")
        host.add_settings_field(key=key, label=label, kind="select", options=_opts(kind),
                                admin_only=True,
                                help="What users get until they pick their own, and what users "
                                     "without the theme.choose permission always get.")

    # ── per-user storage ─────────────────────────────────────────────────
    def _user():
        return host.current_user() or ""

    def _chosen():
        out = {}
        try:
            rows = host.db().execute(
                "SELECT key, value FROM user_prefs WHERE username=? AND key IN (?, ?)",
                (_user(), "theme.functional", "theme.colorings")).fetchall()
            for r in rows:
                out[r["key"].split(".", 1)[1]] = r["value"] or ""
        except Exception:
            pass
        return out

    def _store(kind, value):
        db = host.db()
        if value:
            db.execute("INSERT OR REPLACE INTO user_prefs(username, key, value) VALUES (?, ?, ?)",
                       (_user(), f"theme.{kind}", value))
        else:
            db.execute("DELETE FROM user_prefs WHERE username=? AND key=?", (_user(), f"theme.{kind}"))
        db.commit()

    def _can_choose():
        # Mirror require_feature's rule: admins always may; others need WRITE.
        rec = getattr(g, "user", None) or {}
        if rec.get("is_admin"):
            return True
        return host.core.features.has_level(rec.get("features") or {}, "theme.choose", "write")

    def _effective(chosen, can_choose):
        defaults = {k: (host.config.get(f"theme_default_{k}") or reg.fallback(k)) for k in KINDS}
        sel = {}
        for k in KINDS:
            want = chosen.get(k, "") if can_choose else ""
            sel[k] = want if (want and reg.has(k, want)) else defaults[k]
        return sel, defaults

    def api_theme_get():
        chosen = _chosen()
        can = _can_choose()
        sel, defaults = _effective(chosen, can)
        return jsonify({"success": True, "themes": reg.themes(), "selected": sel,
                        "chosen": chosen, "defaults": defaults, "can_choose": can})

    def api_theme_set():
        d = request.get_json(silent=True) or {}
        for k in KINDS:
            if k not in d:
                continue
            v = str(d.get(k) or "").strip().lower()
            if v and not reg.has(k, v):
                return jsonify({"success": False, "error": f"unknown {k} theme {v!r}"}), 400
            _store(k, v)
        chosen = _chosen()
        sel, defaults = _effective(chosen, True)
        return jsonify({"success": True, "selected": sel, "chosen": chosen, "defaults": defaults,
                        "can_choose": True})

    # GET is login-only (any user needs to know their theme); POST needs the
    # theme.choose permission at write level.
    host.add_route("/api/theme", api_theme_get, methods=["GET"])
    host.add_route("/api/theme", api_theme_set, methods=["POST"], feature="theme.choose",
                   level="write", action="theme_set", fields=("functional", "colorings"))

    host.add_asset("theming.js")
    host.add_asset("theming.css", kind="css")
    host.on_startup(lambda: host.logger.info(
        "theming: %d functional, %d colorings theme(s)",
        len(reg.themes("functional")), len(reg.themes("colorings"))))