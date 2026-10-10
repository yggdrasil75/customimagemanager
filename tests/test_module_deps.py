"""! @file
@brief Every package a module imports is declared in its manifest: "pip" when the
import is unconditional (the module can't load without it), "pip_optional" when
it is guarded (optional_import, try, if, inside a function). The core install
(requirements-ultralight.txt) is the only exception. That is what makes run.sh
install, and the loader report, every module's deps the same way."""
import ast
import os
import sys

import pytest

from modules import loader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = os.path.join(ROOT, "modules")
# requirements-ultralight.txt (+ what flask brings): always installed
CORE = {"flask", "werkzeug", "jinja2", "itsdangerous", "markupsafe", "click", "waitress",
        "numpy", "requests", "yaml", "PIL", "pyexiv2", "ldap3"}
# the app's own top-level files and the core packages' flat aliases
LOCAL = ({f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")} |
         {"modules", "exif_fields", "iptc_fields", "mwg_fields", "xmp_fields", "exif_import",
          "exif_export", "iptc_import", "xmp_import", "xmp_export", "thread_manager", "auth",
          "capabilities", "cimlogger", "features", "sam_common"})


def _imports(tree):
    """! @brief [(top-level package, unconditional?)] for every import in a file."""
    out = []

    def visit(node, top, guarded):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.Import):
                out.extend((a.name.split(".")[0], top and not guarded) for a in ch.names)
            elif isinstance(ch, ast.ImportFrom) and ch.level == 0 and ch.module:
                out.append((ch.module.split(".")[0], top and not guarded))
            elif (isinstance(ch, ast.Call) and getattr(ch.func, "id", None) == "optional_import"
                  and ch.args and isinstance(ch.args[0], ast.Constant)):
                out.append((str(ch.args[0].value).split(".")[0], False))
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                visit(ch, False, guarded)
            else:
                visit(ch, top, guarded or isinstance(ch, (ast.Try, ast.If)))
    visit(tree, True, False)
    return out


def _plugins():
    out = []
    for name in sorted(os.listdir(MODULES)):
        folder = os.path.join(MODULES, name)
        if name in loader._RESERVED_DIRS or name.startswith((".", "_")) or not os.path.isdir(folder):
            continue
        entry = "module.py" if os.path.exists(os.path.join(folder, "module.py")) else "__init__.py"
        if os.path.exists(os.path.join(folder, entry)):
            out.append((name, folder, entry))
    return out


@pytest.mark.parametrize("name,folder,entry", _plugins(), ids=[p[0] for p in _plugins()])
def test_imports_are_declared(name, folder, entry):
    man = loader._manifest_from_source(folder, entry)
    assert man is not None, f"modules/{name}: MANIFEST must be a literal dict (read without importing)"
    names = lambda key: {loader._import_name(a).split(".")[0]
                         for d in man.get(key) or [] for a in loader._alternatives(d)}
    required, optional = names("pip"), names("pip_optional")
    own = {f[:-3] for f in os.listdir(folder) if f.endswith(".py")} | \
          {d for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d))}
    skip = set(sys.stdlib_module_names) | CORE | LOCAL | own
    undeclared, should_be_required = {}, {}
    for dp, dn, fn in os.walk(folder):
        if "tests" in dp.split(os.sep) or "__pycache__" in dp:
            continue
        for f in fn:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dp, f), folder)
            for pkg, hard in _imports(ast.parse(open(os.path.join(dp, f), encoding="utf-8").read())):
                if pkg in skip or pkg in required:
                    continue
                if pkg not in optional:
                    undeclared.setdefault(pkg, rel)
                elif hard:
                    should_be_required.setdefault(pkg, rel)
    assert not undeclared, f"modules/{name} imports packages its manifest doesn't declare: {undeclared}"
    assert not should_be_required, (f"modules/{name} imports these unconditionally, so they belong in "
                                     f"\"pip\", not \"pip_optional\": {should_be_required}")
