"""! @file
@brief Frontend: renders the real `/` page through Flask, snapshots the boot-time
API responses and the enabled modules' asset list, then runs `node --test` once
over tests/js/*.test.js and every modules/<id>/tests/**/*.test.js. Each JS test
file is its own pytest item (a file whose tests all skip - its module is off -
is reported skipped), so the counts and failures say which file. Skips if node
is missing; installs jsdom (the only dependency) into tests/js on first run."""
import glob, json, os, shutil, subprocess
import pytest

JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# GET endpoints the page calls while booting; their real responses become the
# harness defaults so the frontend is exercised against the backend's shapes.
SNAPSHOT = ["/api/state", "/api/modules", "/api/module_assets", "/api/auth/me",
            "/api/auth/config", "/api/models", "/api/ai/actions", "/api/box_labels",
            "/api/encoding/schema"]
FILES = sorted(glob.glob(os.path.join(JS, "*.test.js"))) + \
    sorted(glob.glob(os.path.join(ROOT, "modules", "*", "tests", "**", "*.test.js"), recursive=True))


def _rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


@pytest.fixture(scope="session")
def js_results(client, app):
    """! @brief Run every JS test file in one node process.
    @return ({abs file: [{name, status, error?}]}, stderr) - one JSON line per test
            from tests/js/reporter.mjs.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    if not os.path.isdir(os.path.join(JS, "node_modules", "jsdom")):
        npm = shutil.which("npm")
        if not npm:
            pytest.skip("npm not installed; run `npm install` in tests/js")
        subprocess.run([npm, "install", "--silent", "--no-audit", "--no-fund"], cwd=JS, check=True, timeout=600)
    html = client.get("/").get_data(as_text=True)
    assert "<script" in html
    with open(os.path.join(JS, "_app.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    snap = {}
    for url in SNAPSHOT:
        r = client.get(url)
        if r.status_code == 200 and r.is_json:
            snap[url] = r.get_json()
    with open(os.path.join(JS, "_api_snapshot.json"), "w", encoding="utf-8") as fh:
        json.dump(snap, fh)
    dirs = {lm.id: lm.path for lm in app.module_registry._plugins.values()}
    with open(os.path.join(JS, "_module_assets.json"), "w", encoding="utf-8") as fh:
        json.dump({"assets": snap.get("/api/module_assets", {}).get("assets", []), "dirs": dirs}, fh)
    # NODE_PATH lets module test files `require("cim")` from tests/js.
    env = dict(os.environ, NODE_PATH=JS)
    p = subprocess.run([node, "--test", "--test-reporter=./reporter.mjs", *FILES], cwd=JS,
                       capture_output=True, text=True, timeout=900, env=env)
    out = {}
    for line in p.stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        out.setdefault(os.path.abspath(rec.get("file") or ""), []).append(rec)
    return out, p.stderr


@pytest.mark.parametrize("path", FILES, ids=[_rel(f) for f in FILES])
def test_js_file(js_results, path):
    results, stderr = js_results
    mine = [r for r in results.get(os.path.abspath(path), []) if r.get("nesting", 0) == 0]
    assert mine, f"{_rel(path)} reported no tests (did it fail to load?)\n{stderr[-2000:]}"
    failed = [r for r in mine if r["status"] == "fail"]
    assert not failed, "\n\n".join(f"not ok - {r['name']}\n{r.get('error', '')}" for r in failed)
    if all(r["status"] == "skip" for r in mine):
        pytest.skip(f"all {len(mine)} test(s) skipped (module off or not loaded)")