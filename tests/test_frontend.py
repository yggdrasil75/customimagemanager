"""Frontend: renders the real `/` page through Flask, hands it to the jsdom
harness in tests/js/ and runs `node --test` there. Skips if node is missing.
`npm install` in tests/js once (jsdom is the only dependency)."""
import os, shutil, subprocess
import pytest

JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")


def test_js_suite(client):
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
    p = subprocess.run([node, "--test"], cwd=JS, capture_output=True, text=True, timeout=300)
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
