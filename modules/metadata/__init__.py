"""metadata module — EXIF / IPTC / XMP read/write, and the 3 controls tabs.

Beyond housing the metadata read/write files, this module registers the
EXIF / IPTC / XMP editor TABS into the controls pane via the generic
controls-tab extension area — the module names the tabs it owns; the core
decides where they render. Enabling/disabling or reordering is a matter of
this registration, not a hard-coded template slot.
"""


def register(host):
    # Front-end asset registers the three metadata tabs through
    # window.registerControlsTab (see static/metadata_tabs.js). Backend
    # read/write stays in the exif_/iptc_/xmp_ files this package houses.
    host.add_asset("metadata_tabs.js", module_id="metadata")
