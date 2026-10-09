"""! @file
@brief Version module: VERSION parsing, changelog parser, version compare, the
update check against a fake GitHub, /api/version, the Info "About" section and
the `about` service. Nothing here reaches the network: urlopen is monkeypatched."""
import io
import json
import os
import urllib.error

import pytest

from modules.version import module as vm

CHANGELOG = """# Changelog

Intro text.

## [1.0.0] - 2026-01-02

### Added
- First release.

## [Unreleased]

### Added
- New thing
  that wraps.
- Second thing.

### Fixed
- A bug.

## [0.9.0] - 2025-12-01

### Changed
* Old change.
"""


class _Resp(io.BytesIO):
    """! @brief A minimal urlopen response (context manager over a JSON body)."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(routes, seen):
    """! @brief urlopen stand-in: `routes` maps a URL suffix to a dict body or an HTTP code."""
    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append((url, timeout, req.get_header("User-agent")))
        for suffix, body in routes.items():
            if url.endswith(suffix):
                if isinstance(body, int):
                    raise urllib.error.HTTPError(url, body, "err", {}, None)
                return _Resp(json.dumps(body).encode("utf-8"))
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)
    return urlopen


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """! @brief Point the module's repo-root resolver at an empty temp dir."""
    monkeypatch.setattr(vm, "_root", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def clean_state(host):
    """! @brief Empty version_state before and after a test."""
    db = host.db()
    db.execute("DELETE FROM version_state")
    db.commit()
    yield
    db.execute("DELETE FROM version_state")
    db.commit()


def test_version_file(fake_root):
    assert vm.read_version_file() == "0.0.0-dev"
    (fake_root / "VERSION").write_text("  \n")
    assert vm.read_version_file() == "0.0.0-dev"
    (fake_root / "VERSION").write_text("2.3.4\n")
    assert vm.read_version_file() == "2.3.4"
    info = vm.local_version()
    assert info["version"] == "2.3.4"
    assert info["commit"] == "" and info["channel"] == "release"   # no .git in the temp root


def test_changelog_parser():
    rel = vm.parse_changelog(CHANGELOG)
    assert [r["version"] for r in rel] == ["Unreleased", "1.0.0", "0.9.0"]
    un = rel[0]
    assert un["date"] == ""
    assert un["sections"]["Added"] == ["New thing that wraps.", "Second thing."]
    assert un["sections"]["Fixed"] == ["A bug."]
    assert rel[1]["date"] == "2026-01-02" and rel[1]["sections"] == {"Added": ["First release."]}
    assert rel[2]["sections"]["Changed"] == ["Old change."]
    assert vm.parse_changelog("") == []


def test_real_changelog_parses():
    with open(os.path.join(vm._root(), "CHANGELOG.md"), encoding="utf-8") as fh:
        rel = vm.parse_changelog(fh.read())
    assert rel and rel[0]["version"] == "Unreleased"
    assert rel[0]["sections"].get("Added")


def test_compare():
    assert vm.version_tuple("v1.2.0-dev") == (1, 2, 0)
    assert vm.version_tuple("garbage") == ()
    assert not vm.is_newer("1.0.0-dev", "1.0.0")
    assert not vm.is_newer("1.0.0", "1.0.0-dev")      # -dev is ignored: same release
    assert vm.is_newer("v1.10.0", "1.2.0")
    assert not vm.is_newer("v1.2.0", "1.10.0")
    assert vm.is_newer("1.0.1", "v1.0")
    assert not vm.is_newer("", "1.0.0")


def test_fetch_release(monkeypatch):
    seen = []
    monkeypatch.setattr(vm.urllib.request, "urlopen", _fake_urlopen({
        "/repos/o/r/releases/latest": {"tag_name": "v9.9.9", "html_url": "https://x/rel",
                                       "published_at": "2026-05-01T00:00:00Z"}}, seen))
    out = vm.fetch_latest("o/r")
    assert out["latest_version"] == "v9.9.9" and out["latest_url"] == "https://x/rel"
    assert out["error"] == "" and out["latest_sha"] == ""
    assert seen[0][1] == vm.HTTP_TIMEOUT and seen[0][2] == vm.USER_AGENT


def test_fetch_falls_back_to_commits(monkeypatch):
    seen = []
    monkeypatch.setattr(vm.urllib.request, "urlopen", _fake_urlopen({
        "/repos/o/r/releases/latest": 404,
        "/repos/o/r": {"default_branch": "dev"},
        "/repos/o/r/commits/dev": {"sha": "abcdef1234567", "html_url": "https://x/c",
                                   "commit": {"committer": {"date": "2026-06-01T00:00:00Z"}}}}, seen))
    out = vm.fetch_latest("o/r")
    assert out["latest_sha"] == "abcdef1234567" and out["latest_version"] == ""
    assert out["latest_date"].startswith("2026-06-01") and out["error"] == ""
    assert [u.rsplit("/repos/", 1)[1] for u, _, _ in seen] == ["o/r/releases/latest", "o/r", "o/r/commits/dev"]


def test_fetch_errors_are_stored(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("blocked")
    monkeypatch.setattr(vm.urllib.request, "urlopen", boom)
    out = vm.fetch_latest("o/r")
    assert "blocked" in out["error"]
    monkeypatch.setattr(vm.urllib.request, "urlopen", _fake_urlopen({"/releases/latest": 500}, []))
    assert vm.fetch_latest("o/r")["error"] == "releases: HTTP 500"


def test_api_version_shape(client):
    j = client.get("/api/version").get_json()
    assert j["success"] is True
    for k in ("version", "commit", "date", "channel", "latest", "update_available",
              "checked_at", "error", "repo"):
        assert k in j, k
    assert j["channel"] in ("git", "release")
    assert set(j["latest"]) == {"version", "url", "date", "sha"}


def test_api_version_reads_version_file(client, fake_root):
    (fake_root / "VERSION").write_text("3.1.4")
    assert client.get("/api/version").get_json()["version"] == "3.1.4"


def test_api_changelog(client):
    j = client.get("/api/version/changelog").get_json()
    assert j["success"] and j["releases"][0]["version"] == "Unreleased"
    assert "raw" in j


def test_check_route_with_fake_github(client, monkeypatch, fake_root, clean_state):
    (fake_root / "VERSION").write_text("1.0.0-dev")
    monkeypatch.setattr(vm.urllib.request, "urlopen", _fake_urlopen({
        "/releases/latest": {"tag_name": "v1.1.0", "html_url": "https://x/rel",
                             "published_at": "2026-05-01T00:00:00Z"}}, []))
    j = client.post("/api/version/check", json={}).get_json()
    assert j["success"] and j["latest"]["version"] == "v1.1.0"
    assert j["update_available"] is True and j["checked_at"]
    # cached: a plain GET sees it without the network
    monkeypatch.setattr(vm.urllib.request, "urlopen", None)
    g = client.get("/api/version").get_json()
    assert g["latest"]["version"] == "v1.1.0" and g["update_available"] is True
    (fake_root / "VERSION").write_text("1.1.0")
    assert client.get("/api/version").get_json()["update_available"] is False


def test_check_error_is_cached_not_raised(client, monkeypatch, clean_state):
    def boom(req, timeout=None):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(vm.urllib.request, "urlopen", boom)
    r = client.post("/api/version/check", json={})
    assert r.status_code == 200
    j = r.get_json()
    assert "no network" in j["error"] and j["update_available"] is False


def test_info_sections_has_about(client, monkeypatch, clean_state):
    monkeypatch.setattr(vm.urllib.request, "urlopen", _fake_urlopen({
        "/releases/latest": {"tag_name": "v999.0.0", "html_url": "https://x/rel"}}, []))
    client.post("/api/version/check", json={})
    j = client.get("/api/info").get_json()
    about = [s for s in j["sections"] if s.get("id") == "about"]
    assert len(about) == 1
    rows = {r["label"]: r for r in about[0]["rows"]}
    assert rows["Version"]["value"] == vm.read_version_file()
    assert "update available" in rows["Latest release"]["value"]
    assert rows["Latest release"]["url"] == "https://x/rel"
    assert rows["Last checked"]["value"] != "never"
    assert any(s.get("id") == "search" for s in j["sections"])   # core section still there


def test_about_service_matches_version(host):
    about = host.get_service("about")
    assert about is not None
    assert about["version"] == vm.read_version_file()
    with open(os.path.join(vm._root(), "VERSION"), encoding="utf-8") as fh:
        assert about["version"] == fh.read().strip()
    assert about["channel"] in ("git", "release")
    assert set(("version", "commit", "date", "channel")) <= set(about)


def test_settings_declared(host, app):
    keys = {f["key"]: f for f in host.settings_fields if f.get("module_id") == "version"}
    assert set(keys) == {"version_check_enabled", "version_check_hours", "version_repo"}
    assert all(f["pane"] == "module" for f in keys.values())
    assert app.state.get("version_repo") == vm.DEFAULT_REPO
