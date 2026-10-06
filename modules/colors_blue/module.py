"""Palette: Blue. Sets the accent variables of the core palette contract."""

MANIFEST = {
    "id":          "colors_blue",
    "name":        "Palette: Blue",
    "version":     "1.1.0",
    "description": "The original blue / indigo / sky palette.",
    "core":        False,
    "requires":    ["theming"],
    "pip":         [],
    "assets":      ["colors_blue.css"],
}


def register(host):
    theming = host.get_service("theming")
    if theming is None:
        return
    theming.register("palette", "blue", "Blue", description="The original blue / indigo / sky palette.", default=True)
    host.add_asset("colors_blue.css", kind="css")