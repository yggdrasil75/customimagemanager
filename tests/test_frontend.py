"""Frontend: renders the real `/` page through Flask, snapshots the boot-time
API responses and the enabled modules' asset list, then runs `node --test`
over tests/js/*.test.js and every modules/<id>/tests/*.test.js. Skips if node
is missing; installs jsdom (the only dependency) into tests/js on first run."""
import glob, json, os, shutil, subprocess
import pytest

JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# GET endpoints the page calls while booting; their real responses become the
# harness defaults so the frontend is exercised against the backend's shapes.
SNAPSHOT = ["/api/state", "/api/modules", "/api/module_assets", "/api/auth/me",
            "/api/auth/config", "/api/models", "/api/ai/actions", "/api/box_labels"]


def test_js_suite(client, app):
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
    files = sorted(glob.glob(os.path.join(JS, "*.test.js"))) + \
        sorted(glob.glob(os.path.join(ROOT, "modules", "*", "tests", "*.test.js")))
    # NODE_PATH lets module test files `require("cim")` from tests/js.
    env = dict(os.environ, NODE_PATH=JS)
    p = subprocess.run([node, "--test", *files], cwd=JS, capture_output=True, text=True,
                       timeout=600, env=env)
    # TAP: a failing test is "not ok N - name" followed by an indented YAML
    # block (error, expected/actual, stack). Keep the whole block so the
    # pytest failure says WHY, not just which.
    report, keep = [], False
    for l in p.stdout.splitlines():
        if l.startswith("not ok"):
            keep = True
        elif l.startswith("ok ") or l.startswith("# Subtest"):
            keep = False
        if keep and "duration_ms" not in l and "type: 'test'" not in l:
            report.append(l)
    summary = [l for l in p.stdout.splitlines() if l.startswith("# pass") or l.startswith("# fail")]
    assert p.returncode == 0 and not report, "\n".join(report + summary) + "\n" + p.stderr[-2000:]
    print("\n".join(summary))
