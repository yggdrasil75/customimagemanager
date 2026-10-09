"""! @file
@brief The theme registry and each user's theme.

A layout decides what the interface shows and how; a palette its colours. One
of each is active; theming.js sets body[data-layout] / body[data-palette] and
themes key off those. Themes are modules registering with the `theming` service.
A third axis, the colour scheme (auto / light / dark), is built in: theming.js
resolves "auto" from the browser's prefers-color-scheme and sets
body[data-scheme="light" | "dark"]; static/scheme.css holds the light scheme.

Palette: the user's pick > the admin default > the default palette.
Scheme: the user's pick > the admin default > "auto".
Layout: the user's pick > their account's > their group's > the layout for
their role > the default layout. Picking needs theme.choose at write.
A layout may hide controls, never grant them: permissions are checked
regardless of theme.

GET /api/theme -> {themes, selected, defaults, can_choose}; the picks are the
user settings "layout" / "palette" / "scheme".
"""

from flask import g, jsonify

# Registered by manager.py before any plugin, so theme modules find the service.

KINDS = ("layout", "palette")
SCHEMES = ("auto", "light", "dark")
SCHEME_OPTIONS = [{"value": "auto", "label": "Automatic (follow the browser)"},
                  {"value": "light", "label": "Light"}, {"value": "dark", "label": "Dark"}]
_ID_OK = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


def clean_scheme(v, blank="auto"):
    """! @brief Validate a colour scheme value.
    @param blank  what an empty value means.
    @return "auto", "light" or "dark".
    @throws ValueError for anything else.
    """
    v = str(v or "").strip().lower() or blank
    if v not in SCHEMES:
        raise ValueError(f"unknown scheme {v!r} (auto, light or dark)")
    return v


class ThemeRegistry:
    """! @brief The `theming` service theme modules register into."""

    def __init__(self, host):
        self._host = host
        self._themes = {k: {} for k in KINDS}  # kind -> id -> spec

    def register(self, kind, theme_id, label, *, description="", default=False, roles=()):
        """! @brief Register a theme.
        @param kind      "layout" or "palette".
        @param theme_id  the body attribute value (lowercase letters, digits, _ -).
        @param default   the last-resort fallback.
        @param roles     roles this layout is the default for ("admin" = admins).
        """
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
        """! @brief The default theme of a kind, else the first registered, else ''."""
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

    host.register_feature("theme.choose", "Theme: pick own layout / palette / colour scheme (write=change)",
                          section="settings", section_label="Settings", default="write",
                          role_defaults={"viewer": "write", "uploader": "write"})

    def _valid(kind):
        def check(v):
            v = str(v or "").strip().lower()
            if v and not reg.has(kind, v):
                raise ValueError(f"unknown {kind} {v!r}")
            return v
        return check

    # admin defaults: a palette and a colour scheme (General), a layout per account / group (Users)
    host.add_config_key("theme_default_palette", default="", validate=_valid("palette"), tab="general")
    host.add_settings_field(key="theme_default_palette", label="Default palette", kind="select",
                            section="defaults",
                            options=lambda: reg.options("palette", "(module default)"),
                            help="What users get until they pick their own.")
    host.add_config_key("theme_default_scheme", default="auto", validate=clean_scheme, tab="general")
    host.add_settings_field(key="theme_default_scheme", label="Default colour scheme", kind="select",
                            section="defaults", options=SCHEME_OPTIONS,
                            help="Light, dark, or automatic from each browser's preference, "
                                 "until users pick their own.")
    host.add_account_field("layout", "Layout",
                           options=lambda: reg.options("layout", "(role default)"),
                           help="The layout this account / group starts on. Users with the "
                                "theme.choose permission can change their own.")

    def _user():
        return getattr(g, "user", None) or {}

    def _can_choose():
        u = _user()
        # no user (a public page with auth on) may not choose; host.is_admin() says True there
        return (bool(u) and host.is_admin()) or host.core.features.has_level(
            u.get("features") or {}, "theme.choose", "write")

    def default_palette(_u=None):
        v = host.config.get("theme_default_palette") or ""
        return v if reg.has("palette", v) else reg.fallback("palette")

    def default_scheme(_u=None):
        """! @brief The admin's default colour scheme ("auto" when unset or invalid)."""
        try:
            return clean_scheme(host.config.get("theme_default_scheme"))
        except ValueError:
            return "auto"

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
    host.add_user_setting("scheme", label="Colour scheme", kind="select", feature="theme.choose",
                          default=lambda u: default_scheme(u),
                          validate=lambda v: clean_scheme(v, blank=""),
                          options=SCHEME_OPTIONS, order=12,
                          help="Automatic follows your browser or system light / dark setting.")

    def _selected():
        can = _can_choose()
        defaults = {"layout": default_layout(), "palette": default_palette(),
                    "scheme": default_scheme()}
        sel = {}
        for k in KINDS:
            want = host.user_setting(k) if can else ""
            sel[k] = want if reg.has(k, want) else defaults[k]
        want = host.user_setting("scheme") if can else ""
        sel["scheme"] = want if want in SCHEMES else defaults["scheme"]
        return sel, defaults, can

    def api_theme_get():
        sel, defaults, can = _selected()
        return jsonify({"success": True, "themes": reg.themes(), "selected": sel,
                        "defaults": defaults, "can_choose": can})

    host.add_route("/api/theme", api_theme_get, methods=["GET"])
    host.add_asset("theming.js")
    host.add_asset("theming.css", kind="css")
    host.add_asset("scheme.css", kind="css")
    host.on_startup(lambda: host.logger.info(
        "theming: %d layout(s), %d palette(s)",
        len(reg.themes("layout")), len(reg.themes("palette"))))