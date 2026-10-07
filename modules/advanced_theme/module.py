"""! @file
@brief Layout: Advanced - the full side-by-side layout (gallery beside the editor
and controls pane). It is the stock layout, so it ships no CSS or JS:
registering it makes "Advanced" a pickable layout, the default for admins and
the fallback when nothing else applies."""

MANIFEST = {
    "id":          "advanced_theme",
    "name":        "Layout: Advanced",
    "version":     "1.1.0",
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
    theming.register("layout", "advanced", "Advanced",
                     description="Everything, side by side: gallery, viewer, editor and metadata tabs.",
                     default=True, roles=["admin"])