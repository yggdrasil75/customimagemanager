"""! @file
@brief Loader dependency reporting: alternative deps, and modules whose import
fails on a missing package still report it as a missing pip dep."""
import os
from modules import loader


def test_alternatives_any_one_satisfies():
    dep = "no-such-pkg-xyz:no_such_pkg_xyz|json"
    assert loader._dep_installed(dep)
    assert loader._dep_problem(dep) is None
    assert loader._dep_label(dep) == "no-such-pkg-xyz (or json)"


def test_alternatives_none_installed():
    dep = "no-such-a:no_such_a|no-such-b:no_such_b"
    assert not loader._dep_installed(dep)
    assert loader._dep_problem(dep) == "pip dependency 'no-such-a (or no-such-b)' not installed"

def _fake_pip(monkeypatch, installable):
    """! @brief pip that "installs" only the given packages; returns the argv log."""
    calls, placed = [], set()
    def call(argv):
        calls.append(argv)
        pkgs = argv[argv.index("install") + 1:]
        if all(p in installable for p in pkgs):
            placed.update(pkgs)
            return 0
        return 1
    monkeypatch.setattr(loader.subprocess, "call", call)
    monkeypatch.setattr(loader, "_one_installed",
                        lambda spec: loader._split_dep(spec)[0].strip() in placed)
    return calls


def test_pip_install_picks_first_alternative(monkeypatch):
    calls = _fake_pip(monkeypatch, {"ai-edge-litert", "tflite-runtime"})
    got = loader._pip_install(["ai-edge-litert:ai_edge_litert|tflite-runtime:tflite_runtime"], loader._log)
    assert got == ["ai-edge-litert"] and [c[-1] for c in calls] == ["ai-edge-litert"]


def test_pip_install_falls_back_to_next_alternative(monkeypatch):
    """! @brief No wheel for the preferred package (platform / Python version): the
    next alternative is installed instead of giving up."""
    calls = _fake_pip(monkeypatch, {"tensorflow"})
    got = loader._pip_install(
        ["ai-edge-litert:ai_edge_litert|tflite-runtime:tflite_runtime|tensorflow"], loader._log)
    assert got == ["tensorflow"]
    assert [c[-1] for c in calls] == ["ai-edge-litert", "tflite-runtime", "tensorflow"]


def test_pip_install_one_bad_package_does_not_block_the_rest(monkeypatch):
    calls = _fake_pip(monkeypatch, {"good-a", "good-b"})
    got = loader._pip_install(["good-a", "no-wheel-pkg", "good-b"], loader._log)
    assert got == ["good-a", "good-b"]
    assert calls[0][-3:] == ["good-a", "no-wheel-pkg", "good-b"]       # tried together first


def test_import_failure_reported_as_missing_pip(tmp_path):
    (tmp_path / "module.py").write_text(
        'import nope_crypto_xyz\n'
        'MANIFEST = {"id": "fake_share", "name": "Fake", "pip": ["nope-crypto-xyz:nope_crypto_xyz"]}\n')
    man = loader._manifest_from_source(str(tmp_path), "module.py")
    assert man["id"] == "fake_share" and man["pip"] == ["nope-crypto-xyz:nope_crypto_xyz"]
    err = ModuleNotFoundError("No module named 'nope_crypto_xyz'", name="nope_crypto_xyz")
    assert loader._import_failure(man, err) == "pip dependency 'nope-crypto-xyz' not installed"
    err2 = ModuleNotFoundError("x", name="other_pkg.sub")
    assert "'other_pkg' not installed" in loader._import_failure(man, err2)
    assert loader._import_failure(man, ValueError("x")) is None


def test_family_share_and_movenet_declare_their_deps():
    reg = loader.registry
    fs = reg._plugins.get("family_share")
    assert fs and any("cryptography" in d for d in fs.manifest["pip"])
    mv = reg._plugins.get("movenet")
    assert mv and mv.manifest["pip"] and "ai_edge_litert" in mv.manifest["pip"][0]


def test_theming_is_builtin_core():
    ids = [m["id"] for m in loader.registry.status()]
    core = [m["id"] for m in loader.registry.status() if m["core"] and m["version"] == "builtin"]
    assert "theming" in core and ids.index("theming") < len(core)
    assert "theming" not in loader.registry._plugins