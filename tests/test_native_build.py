"""! @file
@brief native_build.py: the compiled module list holds only Flask-free helper
modules that exist, and --check drops a build whose source changed (by content, not
mtime) or was never recorded."""
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


def _built(tmp_path, monkeypatch, source_hash):
    src = tmp_path / "helper.py"
    src.write_text("x = 1\n")
    so = tmp_path / "helper.cpython-313-x86_64-linux-gnu.so"
    so.write_bytes(b"")
    monkeypatch.setattr(nb, "ROOT", str(tmp_path))
    monkeypatch.setattr(nb, "MANIFEST", str(tmp_path / "data" / "native_manifest.json"))
    monkeypatch.setattr(nb, "COMPILE", ["helper.py"])
    nb._save({"built": {"helper.py": [so.name]},
              "sources": {"helper.py": source_hash(src)} if source_hash else {}})
    return src, so


def test_check_keeps_a_build_of_the_same_source(tmp_path, monkeypatch):
    src, so = _built(tmp_path, monkeypatch, lambda p: nb._digest(str(p)))
    old = time.time() - 100
    os.utime(so, (old, old))                    # older than its source: the content decides
    nb.check()
    assert so.exists()


def test_check_drops_a_build_whose_source_changed_even_with_an_older_mtime(tmp_path, monkeypatch):
    """A file copied or unpacked in keeps its old mtime, older than the stale build."""
    src, so = _built(tmp_path, monkeypatch, lambda p: nb._digest(str(p)))
    src.write_text("x = 2\n")
    old = time.time() - 10000
    os.utime(src, (old, old))
    nb.check()
    assert not so.exists()
    assert "helper.py" not in nb._manifest().get("sources", {})


def test_check_drops_a_build_without_a_recorded_source(tmp_path, monkeypatch):
    src, so = _built(tmp_path, monkeypatch, None)
    nb.check()
    assert not so.exists()