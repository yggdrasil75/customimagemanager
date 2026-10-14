"""! @file
@brief native_build.py: the compiled module list holds only Flask-free helper
modules that exist, and --check drops a build older than its source."""
import os
import time
import native_build as nb


def test_compile_list_covers_the_tree_and_skips_the_entry_points():
    rels = set(nb.COMPILE)
    assert "common.py" in rels and "modules/stats/module.py" in rels and "assets.py" in rels
    for never in ("manager.py", "native_build.py", "conftest.py", "modules/loader.py",
                  "modules/host.py", "modules/__init__.py"):
        assert never not in rels, never
    assert not any(r.startswith("tests/") or "/tests/" in r for r in rels)
    assert "modules/metadata/exif_fields.py" not in rels      # data table: nothing to gain
    for rel in rels:
        assert os.path.exists(os.path.join(nb.ROOT, rel)), rel
    assert nb._dotted("modules/stats/module.py") == "modules.stats.module"
    assert nb._dotted("common.py") == "common"


def test_check_drops_stale_build(tmp_path, monkeypatch):
    src = tmp_path / "helper.py"
    src.write_text("x = 1\n")
    so = tmp_path / "helper.cpython-313-x86_64-linux-gnu.so"
    so.write_bytes(b"")
    old = time.time() - 100
    os.utime(so, (old, old))
    monkeypatch.setattr(nb, "ROOT", str(tmp_path))
    monkeypatch.setattr(nb, "COMPILE", ["helper.py"])
    nb.check()
    assert not so.exists()