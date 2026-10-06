"""Colorings theme: Orange. A palette for the core theming contract."""

MANIFEST = {
    "id":          "colors_orange",
    "name":        "Colours: Orange",
    "version":     "1.0.0",
    "description": "Orange accent with amber and rose secondaries.",
    "core":        False,
    "requires":    ["theming"],
    "pip":         [],
    "assets":      ["colors_orange.css"],
}


def register(host):
    theming = host.get_service("theming")
    if theming is None:
        return
    theming.register("colorings", "orange", "Orange", description="Orange accent with amber and rose secondaries.", default=False)
    host.add_asset("colors_orange.css", kind="css")