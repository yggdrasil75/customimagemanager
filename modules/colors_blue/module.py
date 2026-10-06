"""Colorings theme: Blue (stock). A palette for the core theming contract."""

MANIFEST = {
    "id":          "colors_blue",
    "name":        "Colours: Blue (stock)",
    "version":     "1.0.0",
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
    theming.register("colorings", "blue", "Blue (stock)", description="The original blue / indigo / sky palette.", default=True)
    host.add_asset("colors_blue.css", kind="css")