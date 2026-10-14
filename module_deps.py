#!/usr/bin/env python3
"""! @file
@brief Start-time module dependency check (run.sh / run.bat): installs the pip deps
of enabled modules that declare them, then reports what is still missing."""
import logging

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logging.getLogger("modules.loader").setLevel(logging.INFO)   # pip progress only

from modules import registry  # noqa: E402

got = registry.install_all_deps()
for mid, pk in got.items():
    print(f"installed for {mid}: {' '.join(pk)}")
for mid, pk in registry.missing_pip().items():
    if registry.is_enabled(mid):
        print(f"warn: {mid} still missing {', '.join(pk)} (module will be off)")
for mid, pk in registry.missing_pip(optional=True).items():
    if registry.is_enabled(mid):
        print(f"note: {mid} without optional {', '.join(pk)} (its features that need them are off)")