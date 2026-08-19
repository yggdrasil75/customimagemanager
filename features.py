"""
Feature / permission registry.
======================================================================
A single source of truth for the fine-grained things a user or group can
be allowed or denied. This drives:

  * the effective-permission set the frontend receives (to hide/show UI),
  * server-side enforcement decorators for the matching endpoints,
  * the admin UI's checkbox tree.

A "feature" is an opaque stable string key. UI elements tag themselves
with data-feature="<key>"; the frontend hides any element whose feature
is not in the user's effective set.

Features are grouped into sections purely for display. The whole
"ai_tooling" section can be hidden at once (a section is itself a feature
key), and each item under it can be hidden individually.

Default roles ship as named permission bundles; a "custom" group/user
simply carries its own allow/deny map.
"""

# ── the catalog ─────────────────────────────────────────────────────────────
# section_key: {label, features:[(key,label), ...]}
# A section is toggleable as a whole via its own section_key, and each
# feature under it is individually toggleable.
FEATURE_SECTIONS = {
    "ai_tooling": {
        "label": "AI Tooling",
        "features": [
            ("ai.autotag",      "Auto-Tag Image (YOLO)"),
            ("ai.smarttag",     "Smart Tag (AI pipeline)"),
            ("ai.pose",         "Pose"),
            ("ai.ocr",          "OCR"),
            ("ai.segment",      "Segment (YOLO)"),
            ("ai.barcodes",     "Scan barcodes"),
            ("ai.pose_remove",  "Remove skeleton"),
            ("ai.quicktrain",   "Quick Train"),
            ("ai.trainer",      "Trainer portal link"),
            ("ai.tiers",        "Storage Tiers"),
            ("ai.bg_autotag",   "Background auto-tag when idle"),
            ("ai.reconcile",    "Sync with disk"),
            ("ai.llm",          "LLM actions (✨ AI)"),
            ("ai.iqa",          "Image quality (IQA)"),
        ],
    },
    "fetch": {
        "label": "Fetch (gallery-dl)",
        "features": [],   # section-level toggle only
    },
    "dedup": {
        "label": "Dupes / dedup",
        "features": [],
    },
    "settings": {
        "label": "Settings",
        "features": [
            ("branding", "Branding (name / logo)"),
        ],
    },
    "gallery_tabs": {
        "label": "Gallery tabs",
        "features": [
            ("tab.gallery",  "Gallery tab"),
            ("tab.albums",   "Albums tab"),
            ("tab.albums.edit", "Albums — allow create/delete/rename/add"),
            ("tab.faces",    "Faces tab"),
            ("tab.faces.edit", "Faces — allow editing"),
            ("tab.review",   "Review tab"),
            ("tab.music",    "Music tab"),
            ("tab.books",    "Books tab"),
        ],
    },
    "metadata_tabs": {
        "label": "Metadata editors",
        "features": [
            ("meta.exif",      "EXIF tab"),
            ("meta.exif.edit", "EXIF — allow editing"),
            ("meta.iptc",      "IPTC tab"),
            ("meta.iptc.edit", "IPTC — allow editing"),
            ("meta.xmp",       "XMP tab"),
            ("meta.xmp.edit",  "XMP — allow editing"),
        ],
    },
    "annotations": {
        "label": "Image annotations",
        "features": [
            ("annot.description", "Description — allow editing"),
            ("annot.tags",        "Tags — allow editing"),
            ("annot.boxes",       "Boxes / regions — allow editing"),
            ("data.delete",       "Delete files (single + bulk)"),
            ("data.move",         "Move / relocate files"),
        ],
    },
    "comics": {
        "label": "Comics",
        "features": [
            ("comics.make",   "Make / create comic"),
            ("comics.edit",   "Edit comic pages"),
            ("comics.delete", "Delete comic"),
        ],
    },
}

# Flat list of every leaf feature key.
ALL_FEATURES = [k for s in FEATURE_SECTIONS.values() for k, _ in s["features"]]
# Section keys are themselves toggleable features (hide the whole section).
ALL_SECTION_KEYS = list(FEATURE_SECTIONS.keys())
ALL_KEYS = ALL_SECTION_KEYS + ALL_FEATURES


def _all_allowed():
    return {k: True for k in ALL_KEYS}


def _all_denied():
    return {k: False for k in ALL_KEYS}


# ── built-in role bundles ────────────────────────────────────────────────────
# Each role is a permission map {feature_key: bool}. Missing keys inherit the
# role's implicit default (see ROLE_DEFAULT_ALLOW).
ROLE_DEFAULT_ALLOW = {
    "admin":     True,   # admins get everything regardless (short-circuit)
    "uploader":  False,  # automation account: locked down, opt-in only
    "viewer":    False,  # view-only: nothing
    "custom":    True,    # custom starts open; admin trims from there
}

BUILTIN_ROLES = {
    "admin":    _all_allowed(),
    # uploader: only the pieces an automated box/tag uploader needs.
    "uploader": {**_all_denied(),
                 "ai_tooling": True,
                 "ai.autotag": True,
                 "ai.segment": True},
    # viewer: read-only browsing. Sees all gallery tabs EXCEPT review, and
    # can open Faces but not edit clusters (tab.faces without tab.faces.edit).
    "viewer":   {**_all_denied(),
                 "gallery_tabs": True,
                 "tab.gallery": True,
                 "tab.albums": True,
                 "tab.albums.edit": False,
                 "tab.faces": True,
                 "tab.faces.edit": False,
                 "tab.review": False,
                 "tab.music": True,
                 "tab.books": True,
                 "metadata_tabs": True,
                 "meta.exif": True,
                 "meta.exif.edit": False,
                 "meta.iptc": True,
                 "meta.iptc.edit": False,
                 "meta.xmp": True,
                 "meta.xmp.edit": False,
                 "annot.description": False,
                 "annot.tags": False,
                 "annot.boxes": False,
                 "data.delete": False,
                 "data.move": False,
                 "comics.make": False,
                 "comics.edit": False,
                 "comics.delete": False},
    "custom":   _all_allowed(),
}


def effective_permissions(role, overrides):
    """Resolve the final {feature_key: bool} for a user.

    role       -- one of BUILTIN_ROLES keys (or 'custom')
    overrides  -- per-user dict {feature_key: bool} (may be partial/empty)

    Admins short-circuit to all-allowed. Otherwise: start from the role
    bundle, fall back to the role's implicit default for unmentioned keys,
    then apply the user's own overrides on top.
    """
    if role == "admin":
        return _all_allowed()
    base = dict(BUILTIN_ROLES.get(role, BUILTIN_ROLES["custom"]))
    default = ROLE_DEFAULT_ALLOW.get(role, True)
    out = {}
    for k in ALL_KEYS:
        out[k] = base.get(k, default)
    for k, v in (overrides or {}).items():
        if k in out:
            out[k] = bool(v)
    return out


def catalog():
    """JSON-serializable description of the feature tree for the admin UI."""
    return {
        "sections": [
            {
                "key": skey,
                "label": s["label"],
                "features": [{"key": k, "label": lbl} for k, lbl in s["features"]],
            }
            for skey, s in FEATURE_SECTIONS.items()
        ],
        "roles": list(BUILTIN_ROLES.keys()),
    }