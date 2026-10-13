"""! @file
@brief native_build.py: the compiled module list holds only Flask-free helper
modules that exist, and --check drops a build older than its source."""
import ast
import os
import time
import native_build as nb


def test_compile_list_is_flask_free_and_present():
    for rel in nb.COMPILE:
        path = os.path.join(nb.ROOT, rel)
        assert os.path.exists(path), rel
        tree = ast.parse(open(path).read())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module.split(".")[0])
        assert "flask" not in names and "manager" not in names, rel


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