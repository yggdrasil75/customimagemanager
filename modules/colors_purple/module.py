"""Colorings theme: Purple. A palette for the core theming contract."""

MANIFEST = {
    "id":          "colors_purple",
    "name":        "Colours: Purple",
    "version":     "1.0.0",
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
    theming.register("colorings", "purple", "Purple", description="Violet accent with fuchsia and teal secondaries.", default=False)
    host.add_asset("colors_purple.css", kind="css")