"""Functional theme: Intermediate.

Like Simple (one full-screen pane, pictures open full screen), but Timeline /
Albums / People / Music are tabs down the left, and the viewer's Meta button
shows the Editor and EXIF / IPTC / XMP tabs (the controls pane moves into the
viewer while it is open). Reuses the Simple theme's viewer."""

MANIFEST = {
    "id":          "theme_intermediate",
    "name":        "Interface: Intermediate",
    "version":     "1.0.0",
    "description": "Timeline / Albums / People / Music tabs down the left; pictures open "
                   "full screen with the editor and metadata tabs behind a Meta button.",
    "core":        False,
    "requires":    ["theming", "theme_simple"],
    "pip":         [],
    "assets":      ["theme_intermediate.js", "theme_intermediate.css"],
}


def register(host):
    theming = host.get_service("theming")
    if theming is None:
        return
    theming.register("functional", "intermediate", "Intermediate",
                     description="Timeline / Albums / People / Music tabs on the left; pictures "
                                 "open full screen with the editor and metadata tabs behind Meta.")
    host.add_asset("theme_intermediate.js")
    host.add_asset("theme_intermediate.css", kind="css")