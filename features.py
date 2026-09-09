"""
Feature / permission registry — ORDERED LEVELS.
======================================================================
Each feature carries a LEVEL, not a boolean:  block(0) < read(1) < write(2).

- block : no access (default for a not-logged-in visitor).
- read  : may see/open it (viewer default).
- write : may see AND modify/run it (normal-user default). write implies read.

This replaces allow/deny booleans and the separate ".edit" keys. An endpoint
that shows data needs `read`; one that changes it needs `write`. Every feature
is uniformly block/read/write; for pure actions ("run OCR") the read level is
just unused (grant=write, deny=block).

Overrides may be an explicit level, or symbolic:
  "inherit" -> the feature's declared DEFAULT level (what the module shipped).
  "default" -> the role/group default level.
Resolution: user override, then group override, then role default.
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


# section_key: {label, features:[(key, label, default_level), ...]}
FEATURE_SECTIONS = {
    "ai_tooling": {
        "label": "AI Tooling",
        "features": [
            ("ai.autotag",       "Auto-Tag Image (YOLO)", "write"),
            ("ai.smarttag",      "Smart Tag (AI pipeline)", "write"),
            ("ai.pose",          "Pose", "write"),
            ("ai.ocr",           "OCR", "write"),
            ("ai.segment",       "Segment (YOLO)", "write"),
            ("ai.barcodes",      "Scan barcodes", "write"),
            ("ai.quicktrain",    "Quick Train", "write"),
            ("ai.trainer",       "Trainer portal link", "write"),
            ("ai.trainer.select","Trainer — build/select image sets", "write"),
            ("ai.trainer.keep",  "Trainer — modify persistent sets", "write"),
            ("ai.trainer.run",   "Trainer — start a training run", "write"),
            ("ai.tiers",         "Storage Tiers", "write"),
            ("ai.bg_autotag",    "Background auto-tag when idle", "write"),
            ("ai.reconcile",     "Sync with disk", "write"),
            ("ai.llm",           "LLM actions (✨ AI)", "write"),
            ("ai.iqa",           "Image quality (IQA)", "write"),
        ],
    },
    "fetch":    {"label": "Fetch (gallery-dl)", "features": []},
    "dedup":    {"label": "Dupes / dedup", "features": []},
    "settings": {"label": "Settings",
                 "features": [("branding", "Branding (name / logo)", "write")]},
    "gallery_tabs": {
        "label": "Gallery tabs",
        "features": [
            ("tab.gallery", "Gallery tab", "read"),
            ("tab.albums",  "Albums tab (read=view, write=create/edit)", "read"),
            ("tab.faces",   "Faces tab (read=view, write=edit clusters)", "read"),
            ("tab.review",  "Review tab", "write"),
            ("tab.music",   "Music tab", "read"),
            ("tab.books",   "Books tab (read=view, write=delete)", "read"),
            ("tab.trainer", "Trainer tab", "write"),
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
    "comics": {
        "label": "Comics",
        "features": [
            ("comics.make",   "Make / create comic", "write"),
            ("comics.edit",   "Edit comic pages", "write"),
            ("comics.delete", "Delete comic", "write"),
        ],
    },
}

# Legacy ".edit"/".delete" leaves that collapsed into a base feature's WRITE.
COLLAPSED = {
    "meta.exif.edit":  "meta.exif",
    "meta.iptc.edit":  "meta.iptc",
    "meta.xmp.edit":   "meta.xmp",
    "tab.albums.edit": "tab.albums",
    "tab.faces.edit":  "tab.faces",
    "tab.books.delete":"tab.books",
}


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


def registered_keys():
    return list(ALL_KEYS)


ROLE_DEFAULT_LEVEL = {"admin": WRITE, "uploader": BLOCK, "viewer": READ, "custom": WRITE}

ROLE_LEVELS = {
    "admin":    {},
    "uploader": {"data.upload": WRITE, "ai.autotag": WRITE, "ai.segment": WRITE,
                 "ai_tooling": WRITE},
    "viewer":   {"tab.review": BLOCK, "tab.trainer": BLOCK,
                 "ai.trainer": BLOCK, "ai.trainer.select": BLOCK,
                 "ai.trainer.keep": BLOCK, "ai.trainer.run": BLOCK,
                 "annot.description": READ, "annot.tags": READ, "annot.boxes": READ,
                 "data.delete": BLOCK, "data.move": BLOCK, "data.upload": BLOCK,
                 "comics.make": BLOCK, "comics.edit": BLOCK, "comics.delete": BLOCK},
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
    """Fold legacy {key: bool} overrides into {key: level_name}.
    true->write, false->block; .edit/.delete collapse into base at write."""
    if not old:
        return {}
    out = {}
    for k, v in old.items():
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
