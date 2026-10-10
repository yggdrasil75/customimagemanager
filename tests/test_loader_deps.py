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

def _fake_pip(monkeypatch, installable, pep517_only=()):
    """! @brief pip that "installs" only the given packages; returns the argv log.
    @param pep517_only  packages that only build with --use-pep517 (old setup.py sdists).
    """
    calls, placed = [], set()
    def call(argv):
        calls.append(argv)
        pkgs = [a for a in argv[argv.index("install") + 1:] if a != "--use-pep517"]
        pep517 = "--use-pep517" in argv
        if all(p in installable and (pep517 or p not in pep517_only) for p in pkgs):
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
    # each alternative is tried once plainly (a failed one is retried with --use-pep517)
    assert [c[-1] for c in calls if "--use-pep517" not in c] == ["ai-edge-litert", "tflite-runtime", "tensorflow"]


def test_pip_retries_with_pep517_build(monkeypatch):
    """! @brief An sdist whose legacy setup.py build fails on current distro pythons is
    retried with --use-pep517 and installed."""
    calls = _fake_pip(monkeypatch, {"old-sdist"}, pep517_only={"old-sdist"})
    got = loader._pip_install(["old-sdist:old_sdist"], loader._log)
    assert got == ["old-sdist"]
    assert any("--use-pep517" in c for c in calls)


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

def test_discovery_probes_warn_only_for_enabled_modules(caplog):
    """! @brief Importing every module.py to read manifests doesn't warn about a disabled
    module's missing packages; the loader warns for enabled modules afterwards."""
    import logging
    import optional_deps
    optional_deps._reported.discard("no_such_pkg_disc_a")
    optional_deps._reported.discard("no_such_pkg_disc_b")
    caplog.set_level(logging.WARNING, logger="optional_deps")
    with optional_deps.probing():
        optional_deps.BY_CALLER.setdefault("modules.fake_on.module", [])
        optional_deps.BY_CALLER.setdefault("modules.fake_off.module", [])
        exec("from optional_deps import optional_import\noptional_import('no_such_pkg_disc_a')",
             {"__name__": "modules.fake_on.module"})
        exec("from optional_deps import optional_import\noptional_import('no_such_pkg_disc_b')",
             {"__name__": "modules.fake_off.module"})
    assert "no_such_pkg_disc" not in caplog.text
    assert optional_deps.warn_missing("modules.fake_on") == ["no_such_pkg_disc_a"]
    assert "no_such_pkg_disc_a" in caplog.text and "no_such_pkg_disc_b" not in caplog.text


def test_version_specifiers():
    assert loader._version_ok("5.19.0", "transformers>=5.19")
    assert not loader._version_ok("5.18.2", "transformers>=5.19")
    assert loader._version_ok("6.1.0", "sentence-transformers>=6.1,<7")
    assert not loader._version_ok("7.0", "x>=6.1,<7")
    assert loader._version_ok("2.3.4", "x==2.3.*") and not loader._version_ok("2.4", "x==2.3.*")
    assert loader._dist_name("sentence-transformers>=6.1:sentence_transformers") == "sentence-transformers"
    assert loader._import_name("transformers>=5.19:transformers") == "transformers"
    # installed, but too old: not installed for the loader, and the message says what's there
    assert loader._one_installed("pytest>=1") and not loader._one_installed("pytest>=999")
    assert "(have pytest " in loader._missing_message("pytest>=999")
    assert "requirements-<backend>.txt" in loader._missing_message("torch:no_such_torch_xyz")


def _fake_modules(tmp_path, monkeypatch, mods):
    """! @brief A modules dir of fake plugins {name: (manifest dict, body)}; the package
    path is extended so modules.<name> imports from it."""
    import modules
    for name, (man, body) in mods.items():
        d = tmp_path / name
        d.mkdir()
        (d / "module.py").write_text(f"MANIFEST = {man!r}\n{body}\n")
    monkeypatch.setattr(loader, "_MODULES_DIR", str(tmp_path))
    monkeypatch.setattr(modules, "__path__", list(modules.__path__) + [str(tmp_path)])
    reg = loader.ModuleRegistry()
    reg.discover()
    return reg


class _Host:
    def __init__(self):
        import logging
        self.logger = logging.getLogger("test.loader")
        self.calls = []
        self._current_module = None


def test_each_enabled_module_goes_through_the_same_steps(tmp_path, monkeypatch, caplog):
    import logging, sys
    boom = "raise RuntimeError('module code ran')"
    reg = _fake_modules(tmp_path, monkeypatch, {
        "fk_off":  ({"id": "fk_off", "pip": []}, boom),                          # disabled
        "fk_need": ({"id": "fk_need", "pip": ["no-such-pkg-q:no_such_pkg_q"]}, boom),  # required dep missing
        "fk_old":  ({"id": "fk_old", "pip": ["pytest>=999"]}, boom),           # too old
        "fk_opt":  ({"id": "fk_opt", "pip": [], "pip_optional": ["no-such-opt-q:no_such_opt_q"]},
                    "def register(host):\n    host.calls.append('fk_opt')"),
        "fk_ok":   ({"id": "fk_ok", "pip": ["pytest"]},
                    "def register(host):\n    host.calls.append('fk_ok')"),
    })
    # discovery read every manifest and ran nothing
    assert set(reg._plugins) == {"fk_off", "fk_need", "fk_old", "fk_opt", "fk_ok"}
    assert all(lm.py_module is None for lm in reg._plugins.values())
    reg.init_state({"fk_off": False})
    host = _Host()
    caplog.set_level(logging.INFO, logger="test.loader")
    reg.register_all(host)
    assert host.calls == ["fk_ok", "fk_opt"] or sorted(host.calls) == ["fk_ok", "fk_opt"]
    assert "modules.fk_off.module" not in sys.modules            # disabled: never imported
    assert reg._plugins["fk_need"].py_module is None             # missing dep: code never ran
    assert reg._plugins["fk_need"].error == "pip dependency 'no-such-pkg-q' not installed"
    assert "have pytest" in reg._plugins["fk_old"].error
    assert "module 'fk_need' disabled: pip dependency 'no-such-pkg-q' not installed" in caplog.text
    assert "module 'fk_opt': optional dependency 'no-such-opt-q' not installed" in caplog.text
    assert "fk_off" not in caplog.text
    # run.sh's lists: enabled or not, by kind
    assert reg.missing_pip()["fk_need"] == ["no-such-pkg-q"]
    assert reg.missing_pip(optional=True)["fk_opt"] == ["no-such-opt-q"]


def test_install_covers_required_and_optional_of_enabled_modules_only(tmp_path, monkeypatch):
    reg = _fake_modules(tmp_path, monkeypatch, {
        "fk_a": ({"id": "fk_a", "pip": ["no-a-q:no_a_q"], "pip_optional": ["no-b-q:no_b_q"]}, ""),
        "fk_c": ({"id": "fk_c", "pip": ["no-c-q:no_c_q"]}, ""),
    })
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"modules": {"fk_c": false}}')
    asked = []
    monkeypatch.setattr(loader, "_pip_install", lambda deps, log: asked.extend(deps) or [])
    reg.install_all_deps(str(cfg))
    assert asked == ["no-a-q:no_a_q", "no-b-q:no_b_q"]
