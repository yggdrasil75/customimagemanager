"""
Theming — the core theme registry and per-user theme selection.

A theme is one of two kinds:

  layout    WHAT the interface shows and how it is laid out (which panes, how
            a picture opens, how big things are). Exactly one is active.
  palette   WHAT COLOUR it is. Exactly one is active.

This module owns the mechanism only; the themes themselves are modules that
register with the `theming` service (see modules/README.md, "Themes"). The
front end (theming.js) sets body[data-layout="<id>"] and
body[data-palette="<id>"]; a theme's CSS/JS keys off those attributes.

Which theme a user gets
  palette  their own pick (User settings) > the admin default (Settings →
           General) > the palette registered with default=True.
  layout   their own pick > what an admin set on their account > on their
           group (Settings → Users) > the layout registered for their role
           (roles=[...]) > the layout registered with default=True.
Picking one's own needs the theme.choose permission at write.

Permissions. A layout is presentation: it may hide controls a user is
allowed to use, never show ones they are not. The permission layer
(features.js on the client, require_feature on every route) is unaffected by
which theme is active, and theming.js re-applies feature visibility after
every switch.

Routes
  GET /api/theme  -> {themes: {layout: [...], palette: [...]},
                      selected: {layout, palette}, defaults: {...}, can_choose}
The user's picks are the per-user settings "layout" / "palette"
(POST /api/user/settings).
"""

from flask import g, jsonify

# Built-in core module: not discovered by the loader (modules/loader.py lists
# it in _CORE and _RESERVED_DIRS); manager.py calls register(host) right after
# metadata and threading, before any plugin, so theme modules find the
# `theming` service when they register.

KINDS = ("layout", "palette")
_ID_OK = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


class ThemeRegistry:
    """What theme modules register into; published as the `theming` service."""

    def __init__(self, host):
        self._host = host
        self._themes = {k: {} for k in KINDS}     # kind -> id -> spec

    def register(self, kind, theme_id, label, *, description="", default=False, roles=()):
        """Declare a theme. kind is "layout" or "palette"; theme_id is the value
        the body attribute takes (lowercase letters, digits, _ -). default=True
        makes it the last-resort fallback; roles=("viewer", …) makes it the
        default for accounts with those roles (layouts; "admin" = admins)."""
        if kind not in KINDS:
            raise ValueError(f"theme kind must be one of {KINDS}")
        theme_id = str(theme_id or "").strip().lower()
        if not theme_id or set(theme_id) - _ID_OK:
            raise ValueError("theme id: lowercase letters, digits, _ and - only")
        self._themes[kind][theme_id] = {
            "id": theme_id, "label": label or theme_id, "description": description or "",
            "default": bool(default), "roles": [str(r) for r in roles or ()],
            "module_id": self._host._current_module,
        }
        return theme_id

    def themes(self, kind=None):
        if kind:
            return list(self._themes[kind].values())
        return {k: list(v.values()) for k, v in self._themes.items()}

    def has(self, kind, theme_id):
        return bool(theme_id) and theme_id in self._themes.get(kind, {})

    def for_role(self, kind, role):
        for t in self._themes[kind].values():
            if role in t["roles"]:
                return t["id"]
        return ""

    def fallback(self, kind):
        """The flagged default, else the first registered, else '' (no theme ->
        the body attribute is left unset)."""
        ts = list(self._themes[kind].values())
        for t in ts:
            if t["default"]:
                return t["id"]
        return ts[0]["id"] if ts else ""

    def options(self, kind, blank=None):
        out = [{"value": "", "label": blank}] if blank else []
        return out + [{"value": t["id"], "label": t["label"]} for t in self._themes[kind].values()]


def register(host):
    reg = ThemeRegistry(host)
    host.provide_service("theming", reg)

    host.register_feature("theme.choose", "Theme: pick own layout / palette (write=change)",
                          section="settings", section_label="Settings", default="write",
                          role_defaults={"viewer": "write", "uploader": "write"})

    def _valid(kind):
        def check(v):
            v = str(v or "").strip().lower()
            if v and not reg.has(kind, v):
                raise ValueError(f"unknown {kind} {v!r}")
            return v
        return check

    # Admin: one default palette for everyone (General), a default layout per
    # account / group (Users). Role defaults come from the layout modules.
    host.add_config_key("theme_default_palette", default="", validate=_valid("palette"), tab="general")
    host.add_settings_field(key="theme_default_palette", label="Default palette", kind="select",
                            section="defaults",
                            options=lambda: reg.options("palette", "(module default)"),
                            help="What users get until they pick their own.")
    host.add_account_field("layout", "Layout",
                           options=lambda: reg.options("layout", "(role default)"),
                           help="The layout this account / group starts on. Users with the "
                                "theme.choose permission can change their own.")

    def _user():
        return getattr(g, "user", None) or {}

    def _can_choose():
        u = _user()
        return bool(u.get("is_admin")) or host.core.features.has_level(
            u.get("features") or {}, "theme.choose", "write")

    def default_palette(_u=None):
        v = host.config.get("theme_default_palette") or ""
        return v if reg.has("palette", v) else reg.fallback("palette")

    def default_layout(u=None):
        u = u if u is not None else _user()
        v = (u.get("account") or {}).get("layout", "")
        if reg.has("layout", v):
            return v
        return reg.for_role("layout", u.get("effective_role") or "") or reg.fallback("layout")

    host.add_user_setting("layout", label="Layout", kind="select", feature="theme.choose",
                          default=lambda u: default_layout(u), validate=_valid("layout"),
                          options=lambda: reg.options("layout"), order=10,
                          help="How much of the interface to show. Changes how things look, "
                               "never what your account may do.")
    host.add_user_setting("palette", label="Palette", kind="select", feature="theme.choose",
                          default=lambda u: default_palette(u), validate=_valid("palette"),
                          options=lambda: reg.options("palette"), order=11)

    def _selected():
        can = _can_choose()
        defaults = {"layout": default_layout(), "palette": default_palette()}
        sel = {}
        for k in KINDS:
            want = host.user_setting(k) if can else ""
            sel[k] = want if reg.has(k, want) else defaults[k]
        return sel, defaults, can

    def api_theme_get():
        sel, defaults, can = _selected()
        return jsonify({"success": True, "themes": reg.themes(), "selected": sel,
                        "defaults": defaults, "can_choose": can})

    host.add_route("/api/theme", api_theme_get, methods=["GET"])
    host.add_asset("theming.js")
    host.add_asset("theming.css", kind="css")
    host.on_startup(lambda: host.logger.info(
        "theming: %d layout(s), %d palette(s)",
        len(reg.themes("layout")), len(reg.themes("palette"))))