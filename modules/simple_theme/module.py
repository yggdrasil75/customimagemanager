"""Functional theme: Simple.

The timeline (with the albums across the top) is the whole screen. Clicking a
picture opens it full screen with the people in it, a Meta button for the
description / tags / albums, and prev / next (buttons or arrow keys).

This module also ships the shared full-screen viewer (simple_viewer.js,
window.CIMSimpleViewer) that the Intermediate theme reuses; the simple theme
itself activates it in "simple" meta mode (theme_simple.js).
"""

MANIFEST = {
    "id":          "theme_simple",
    "name":        "Interface: Simple",
    "version":     "1.0.0",
    "description": "Timeline + albums fill the screen; a picture opens full screen with "
                   "people, a Meta button and prev / next. For someone who just wants to look.",
    "core":        False,
    "requires":    ["theming", "timeline"],
    "pip":         [],
    "assets":      ["simple_viewer.js", "simple_viewer.css", "theme_simple.js", "theme_simple.css"],
}


def register(host):
    theming = host.get_service("theming")
    if theming is None:
        return
    theming.register("functional", "simple", "Simple",
                     description="Timeline and albums fill the screen; pictures open full screen "
                                 "with people, Meta and prev / next.")
    host.add_asset("simple_viewer.js")
    host.add_asset("simple_viewer.css", kind="css")
    host.add_asset("theme_simple.js")
    host.add_asset("theme_simple.css", kind="css")