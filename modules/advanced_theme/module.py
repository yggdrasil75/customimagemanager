"""Functional theme: Advanced — the full side-by-side layout (gallery beside
the editor and controls pane). It is the stock layout, so it ships no CSS or
JS: registering it simply makes "Advanced" a pickable interface and the
fallback when the admin set no default."""

MANIFEST = {
    "id":          "theme_advanced",
    "name":        "Interface: Advanced",
    "version":     "1.0.0",
    "description": "Everything, side by side: gallery, viewer, editor and metadata tabs.",
    "core":        False,
    "requires":    ["theming"],
    "pip":         [],
    "assets":      [],
}


def register(host):
    theming = host.get_service("theming")
    if theming is None:
        return
    theming.register("functional", "advanced", "Advanced",
                     description="Everything, side by side: gallery, viewer, editor and metadata tabs.",
                     default=True)