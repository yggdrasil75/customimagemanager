"""! @file
@brief Feature permissions with ordered levels: block < read < write.

read lets a user see or open a feature, write lets them change or run it
(write implies read). An override is a level, "inherit" (the feature's
declared default) or "default" (the role's default); a user override beats a
group override, which beats the role default.
"""

BLOCK, READ, WRITE = 0, 1, 2
LEVELS = {"block": BLOCK, "read": READ, "write": WRITE}
LEVEL_NAMES = {BLOCK: "block", READ: "read", WRITE: "write"}


def level_of(v, fallback=BLOCK):
    if isinstance(v, bool):
        return WRITE if v else BLOCK
    if isinstance(v, int):
        return max(BLOCK, min(WRITE, v))
    if isinstance(v, str) and v in LEVELS:
        return LEVELS[v]
    return fallback


# section key -> {label, features: [(key, label, default level), ...]}
FEATURE_SECTIONS = {
    "ai_tooling": {
        "label": "AI Tooling",
        "features": [
            ("ai.autotag",       "Detect objects (AI picker: Detection class)", "write"),
        ],
    },
    "library": {
        "label": "Library maintenance",
        "features": [("library.reconcile", "Sync with disk", "write")],
    },
    "settings": {"label": "Settings",
                 "features": [("branding", "Branding (name / logo)", "write")]},
    "gallery_tabs": {
        "label": "Gallery tabs",
        "features": [
            ("tab.gallery", "Gallery tab", "read"),
            ("tab.albums",  "Albums tab (read=view, write=create/edit)", "read"),
            ("tab.review",  "Review tab", "write"),
        ],
    },
    "annotations": {
        "label": "Image annotations",
        "features": [
            ("annot.description", "Description (write=edit)", "write"),
            ("annot.tags",        "Tags (write=edit)", "write"),
            ("annot.boxes",       "Boxes / regions (write=edit)", "write"),
            ("data.delete",       "Delete files (single + bulk)", "write"),
            ("data.move",         "Move / relocate files", "write"),
            ("data.upload",       "Upload / drag-drop files", "write"),
        ],
    },
    "viewers": {"label": "Viewers",
                "features": [("view.3d", "3D viewer (mesh / body)", "read")]},
}

# Legacy ".edit" keys folded into the WRITE level of their base feature.
COLLAPSED = {"tab.albums.edit": "tab.albums"}
RENAMED = {"ai.tiers": "settings.storage", "settings.tiers": "settings.storage",
           "ai.reconcile": "library.reconcile"}


def _rebuild():
    global FEATURE_DEFAULTS, ALL_FEATURES, ALL_SECTION_KEYS, ALL_KEYS
    FEATURE_DEFAULTS = {}
    for skey, s in FEATURE_SECTIONS.items():
        FEATURE_DEFAULTS[skey] = READ
        for key, _lbl, dflt in s["features"]:
            FEATURE_DEFAULTS[key] = level_of(dflt, READ)
    ALL_FEATURES = [k for s in FEATURE_SECTIONS.values() for k, _, _ in s["features"]]
    ALL_SECTION_KEYS = list(FEATURE_SECTIONS.keys())
    ALL_KEYS = ALL_SECTION_KEYS + ALL_FEATURES


FEATURE_DEFAULTS = {}
ALL_FEATURES = ALL_SECTION_KEYS = ALL_KEYS = []
_rebuild()


def register_section(section_key, label):
    if section_key not in FEATURE_SECTIONS:
        FEATURE_SECTIONS[section_key] = {"label": label, "features": []}
        _rebuild()
    return section_key


def register_feature(key, label, *, section="modules", section_label="Modules",
                     default="write", role_defaults=None):
    register_section(section, section_label)
    feats = FEATURE_SECTIONS[section]["features"]
    dflt = level_of(default, WRITE)
    for i, (k, _lbl, _d) in enumerate(feats):
        if k == key:
            feats[i] = (key, label, dflt); break
    else:
        feats.append((key, label, dflt))
    _rebuild()
    for role, bundle in ROLE_LEVELS.items():
        if role == "admin":
            continue
        rd = (role_defaults or {}).get(role)
        if rd is not None:
            bundle[key] = level_of(rd, dflt)
    return key


def settings_tab_feature(tab_id):
    """! @brief The permission key of a Settings tab."""
    return "settings." + str(tab_id)


# Non-admin levels for a Settings tab: admin-only tabs start blocked, others readable.
_TAB_ROLE_DEFAULTS = {
    "admin_only": {"viewer": "block", "uploader": "block", "custom": "block"},
    "normal":     {"viewer": "block", "uploader": "block"},
    "public":     {"viewer": "read", "uploader": "read"},
}


def register_settings_tab(tab_id, label, *, admin_only=False, public=False, default="read"):
    """! @brief Register the permission of one Settings tab (core or module).
    read shows the tab; write lets its settings be saved.
    """
    kind = "admin_only" if admin_only else ("public" if public else "normal")
    return register_feature(settings_tab_feature(tab_id), f"Settings: {label} tab",
                            section="settings", section_label="Settings",
                            default=default, role_defaults=_TAB_ROLE_DEFAULTS[kind])


def registered_keys():
    return list(ALL_KEYS)


ROLE_DEFAULT_LEVEL = {"admin": WRITE, "uploader": BLOCK, "viewer": READ, "custom": WRITE}

ROLE_LEVELS = {
    "admin":    {},
    "uploader": {"data.upload": WRITE, "ai.autotag": WRITE, "ai.segment": WRITE,
                 "ai_tooling": WRITE},
    "viewer":   {"tab.review": BLOCK,
                 "annot.description": READ, "annot.tags": READ, "annot.boxes": READ,
                 "data.delete": BLOCK, "data.move": BLOCK, "data.upload": BLOCK,
                 },
    "custom":   {},
}


def _role_level(role, key):
    if role == "admin":
        return WRITE
    bundle = ROLE_LEVELS.get(role, ROLE_LEVELS["custom"])
    if key in bundle:
        return bundle[key]
    rdefault = ROLE_DEFAULT_LEVEL.get(role, WRITE)
    fdefault = FEATURE_DEFAULTS.get(key, READ)
    return min(rdefault, fdefault)


def resolve_level(role, key, user_override=None, group_override=None):
    if role == "admin":
        return WRITE

    def _apply(ov):
        if ov is None or ov == "default":
            return None
        if ov == "inherit":
            return FEATURE_DEFAULTS.get(key, READ)
        return level_of(ov, FEATURE_DEFAULTS.get(key, READ))

    u = _apply(user_override) if user_override is not None else None
    if u is not None:
        return u
    grp = _apply(group_override) if group_override is not None else None
    if grp is not None:
        return grp
    return _role_level(role, key)


def effective_permissions(role, overrides, group_overrides=None):
    if role == "admin":
        return {k: WRITE for k in ALL_KEYS}
    overrides = overrides or {}
    group_overrides = group_overrides or {}
    return {k: resolve_level(role, k, overrides.get(k), group_overrides.get(k))
            for k in ALL_KEYS}


def has_level(perms, key, need=READ):
    if isinstance(need, str):
        need = LEVELS.get(need, READ)
    return level_of(perms.get(key, BLOCK), BLOCK) >= need


def migrate_perms(old):
    """! @brief Convert legacy {key: bool} overrides to {key: level name}.
    true -> write, false -> block; ".edit" / ".delete" keys fold into their base.
    """
    if not old:
        return {}
    out = {}
    for k, v in old.items():
        k = RENAMED.get(k, k)
        if k in ("ai.bg_autotag",):  # removed feature
            continue
        if k in COLLAPSED:
            if level_of(v) >= WRITE:
                out[COLLAPSED[k]] = "write"
            continue
        if isinstance(v, bool):
            out[k] = "write" if v else "block"
        else:
            out[k] = v if isinstance(v, str) else LEVEL_NAMES.get(level_of(v), "block")
    return out


def catalog():
    return {
        "levels": ["block", "read", "write", "inherit", "default"],
        "sections": [
            {"key": skey, "label": s["label"],
             "features": [{"key": k, "label": lbl,
                           "default": LEVEL_NAMES.get(dflt, "read")}
                          for k, lbl, dflt in s["features"]]}
            for skey, s in FEATURE_SECTIONS.items()
        ],
        "roles": list(ROLE_DEFAULT_LEVEL.keys()),
    }


# The core tabs. "user" has no permission: every signed-in user edits their own.
for _tid, _lbl, _kw in (("general", "General", {}), ("media", "Media", {}),
                        ("storage", "Storage", {}), ("models", "Models", {}),
                        ("info", "Info", {"public": True}),
                        ("users", "Users", {"admin_only": True}),
                        ("modules", "Modules", {"admin_only": True})):
    register_settings_tab(_tid, _lbl, **_kw)