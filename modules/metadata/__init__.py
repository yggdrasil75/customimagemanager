"""metadata module — EXIF / IPTC / XMP read/write, editors, and controls tabs.

Owns the metadata editors end to end: the read/write Python (exif_/iptc_/xmp_
files here), the editor JS + CSS (modules/metadata/static, contributed as
assets), and the editor HTML panes (modules/metadata/templates, contributed as
server-rendered partials). Nothing metadata-specific lives in core: no template
{% include %}, no static <script>/<link>, no hard-coded tab. A module ships all
of this and registers it; core renders it without knowing it exists.
"""


def register(host):
    # Editor JS + CSS as front-end assets.
    for name in ("exif_editor", "iptc_editor", "xmp_editor"):
        host.add_asset(f"{name}.css", kind="css", module_id="metadata")
        host.add_asset(f"{name}.js", kind="js", module_id="metadata")
    host.add_asset("metadata_tabs.js", kind="js", module_id="metadata")

    # Editor panes as SERVER-RENDERED partials (from this module's templates/
    # dir, on Jinja's search path). Each is paired with a controls tab that
    # metadata_tabs.js registers on the front end.
    host.register_controls_pane("exif", "exif_editor.html", feature="meta.exif")
    host.register_controls_pane("iptc", "iptc_editor.html", feature="meta.iptc")
    host.register_controls_pane("xmp",  "xmp_editor.html",  feature="meta.xmp")
