"""Palette: Purple. Sets the accent variables of the core palette contract."""

MANIFEST = {
    "id":          "colors_purple",
    "name":        "Palette: Purple",
    "version":     "1.1.0",
    "description": "Violet accent with fuchsia and teal secondaries.",
    "core":        False,
    "requires":    ["theming"],
    "pip":         [],
    "assets":      ["colors_purple.css"],
}


def register(host):
    theming = host.get_service("theming")
    if theming is None:
        return
    theming.register("palette", "purple", "Purple", description="Violet accent with fuchsia and teal secondaries.", default=False)
    host.add_asset("colors_purple.css", kind="css")