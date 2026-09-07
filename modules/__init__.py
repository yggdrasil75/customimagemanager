"""
modules package — the module system + the app's core building blocks.
======================================================================

This package does TWO distinct things, kept deliberately separate:

1. A REAL PLUGIN SYSTEM (loader.py + host.py). Third-party modules are
   folders under modules/ that ship a manifest and a register(host)
   function. At startup the loader discovers them, orders them by their
   declared dependencies, and calls register(host) on the enabled ones so
   they wire themselves into the app through the Host surface. Publish a
   folder, drop it in modules/, restart — it integrates. See
   modules/example_hello/ for a working, self-contained example, and
   modules/README.md for the authoring contract.

2. HOUSING THE CORE BUILDING BLOCKS. auth, capabilities, metadata, and
   threading were moved here into subfolders as plain filesystem tidying.
   These are NOT yet loaded through the plugin system — manager.py still
   imports them directly. To avoid rewriting hundreds of call sites in the
   same commit as the move, importing this package aliases each moved file
   back to its old flat name in sys.modules, so `import auth` /
   `import exif_import` keep resolving. This aliasing is a migration
   convenience for the core files ONLY; it is not how plugins load.
   Converting a core building block to load via register(host) is a later
   section — the seam for it now exists.

cimlogger.py never moved (many non-core files import it, no optional deps);
it is aliased in place and listed as a core module in the UI.
"""

import sys
import importlib

from .loader import registry  # noqa: F401  the ModuleRegistry singleton
from . import host  # noqa: F401  Host class, for manager.py to construct
from .model_broker import broker  # noqa: F401  the ModelBroker singleton
from .model_contracts import declare_core_capabilities

# The core owns the initial capability contracts (box.faces, box.objects,
# segment, pose). Declare them before any module registers providers.
declare_core_capabilities(broker)


def _alias(dotted, legacy):
    """Import `dotted` and also register it under the flat `legacy` name.

    After this, both `import modules.metadata.exif_import` and the legacy
    `import exif_import` return the same module object.
    """
    mod = importlib.import_module(dotted)
    sys.modules.setdefault(legacy, mod)
    # setdefault: if something already imported the legacy name first (e.g. a
    # test), don't clobber it — the first bound object wins and stays identical.
    return mod


# ── logging first: everyone depends on it, it has no heavy deps ──────────────
# cimlogger.py stays at repo root; alias is a no-op that just confirms presence.
_alias("cimlogger", "cimlogger")

# ── capabilities: auth imports it ────────────────────────────────────────────
_alias("modules.capabilities.capabilities", "capabilities")

# ── metadata: fields before the importers/exporters that reference them ──────
_alias("modules.metadata.exif_fields", "exif_fields")
_alias("modules.metadata.iptc_fields", "iptc_fields")
_alias("modules.metadata.mwg_fields",  "mwg_fields")
_alias("modules.metadata.xmp_fields",  "xmp_fields")   # imports iptc_fields, mwg_fields
_alias("modules.metadata.exif_import", "exif_import")
_alias("modules.metadata.exif_export", "exif_export")
_alias("modules.metadata.iptc_import", "iptc_import")
_alias("modules.metadata.xmp_import",  "xmp_import")
_alias("modules.metadata.xmp_export",  "xmp_export")

# ── threading ────────────────────────────────────────────────────────────────
_alias("modules.threading.thread_manager", "thread_manager")

# ── auth last: it imports capabilities + cimlogger ───────────────────────────
_alias("modules.auth.auth", "auth")

# ── discover pluggable modules on disk ───────────────────────────────────────
# This imports each plugin folder's manifest (module.py / __init__.py) but does
# NOT call register() yet — manager.py does that after it has built the Host.
# Import failures are recorded per-module, never raised, so one broken plugin
# can't stop the app. See modules/loader.py.
registry.discover()
