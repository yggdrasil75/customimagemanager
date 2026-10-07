"""! @file
@brief The module system (loader, host, broker, config registry) and the core
building blocks (auth, capabilities, metadata, threading, theming, encoding).

Core files are also registered under their old flat names (`import auth`,
`import exif_import`) so existing imports keep working. Plugins are discovered
here and registered later by manager.py (see loader.py, README.md).
"""

import sys
import importlib

from .loader import registry  # noqa: F401
from . import host  # noqa: F401
from .model_broker import broker  # noqa: F401
from .config_registry import config  # noqa: F401
from .model_contracts import declare_core_capabilities

# core capability contracts, before any provider registers
declare_core_capabilities(broker)


def _alias(dotted, legacy):
    """! @brief Import `dotted` and register the same module object under `legacy`."""
    mod = importlib.import_module(dotted)
    sys.modules.setdefault(legacy, mod)
    # keep a module already imported under the legacy name
    return mod


_alias("cimlogger", "cimlogger")
_alias("modules.capabilities.capabilities", "capabilities")
_alias("modules.metadata.exif_fields", "exif_fields")
_alias("modules.metadata.iptc_fields", "iptc_fields")
_alias("modules.metadata.mwg_fields",  "mwg_fields")
_alias("modules.metadata.xmp_fields",  "xmp_fields")  # imports iptc_fields, mwg_fields
_alias("modules.metadata.exif_import", "exif_import")
_alias("modules.metadata.exif_export", "exif_export")
_alias("modules.metadata.iptc_import", "iptc_import")
_alias("modules.metadata.xmp_import",  "xmp_import")
importlib.import_module("modules.encoding")
_alias("modules.metadata.xmp_export",  "xmp_export")
_alias("modules.threading.thread_manager", "thread_manager")
from . import theming  # noqa: F401,E402
_alias("modules.auth.auth", "auth")

# Read every plugin's manifest; register() runs later. A broken plugin is recorded, not raised.
registry.discover()
