"""! @file
@brief Layout: Intermediate.

Like Simple (one full-screen pane, pictures open full screen), but Timeline /
Albums / People / Music are tabs down the left, and the viewer's Meta button
shows the Editor and EXIF / IPTC / XMP tabs (the controls pane moves into the
viewer while it is open). Reuses the Simple layout's viewer; its "people in
this photo" chips become avatar chips that, with the People module on, show
every photo of that person (person:<id>) and offer "Change person"."""

MANIFEST = {
    "id":          "intermediate_theme",
    "name":        "Layout: Intermediate",
    "version":     "1.1.1",
    "description": "Timeline / Albums / People / Music tabs down the left; pictures open "
                   "full screen with the editor and metadata tabs behind a Meta button.",
    "core":        False,
    "requires":    ["theming", "simple_theme"],
    "pip":         [],
    "assets":      ["theme_intermediate.js", "theme_intermediate.css"],
}


def register(host):
    theming = host.get_service("theming")
    if theming is None:
        return
    theming.register("layout", "intermediate", "Intermediate",
                     description="Timeline / Albums / People / Music tabs on the left; pictures "
                                 "open full screen with the editor and metadata tabs behind Meta.")
    host.add_asset("theme_intermediate.js")
    host.add_asset("theme_intermediate.css", kind="css")