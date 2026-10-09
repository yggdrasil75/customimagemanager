"""! @file
@brief Version & updates module: the install's version, its changelog and an update check.

The module reads `VERSION` at the repo root (plus the git short hash and commit
date when the install is a checkout), parses `CHANGELOG.md` into releases and
checks GitHub for a newer release, falling back to the default branch's latest
commit when the repository has no releases. The check runs in a background
thread after startup and then every `version_check_hours`; its result is
cached in the `version_state` table so a request never waits on the network.
Everything is exposed under /api/version, as an "About" section in
Settings -> Info and as the `about` service other modules (stats) read.
"""
import os
import re
import json
import time
import threading
import subprocess
import urllib.request
import urllib.error

from flask import jsonify

MANIFEST = {
    "id":          "version",
    "name":        "Version & updates",
    "version":     "1.0.0",
    "description": "Shows the installed version and changelog, publishes the `about` "
                   "service and checks GitHub for a newer release.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["version.js"],
}

FEATURE = "version"
DEFAULT_REPO = "yggdrasil75/customimagemanager"
FALLBACK_VERSION = "0.0.0-dev"
USER_AGENT = "customimagemanager-version-check"
HTTP_TIMEOUT = 10
GIT_TIMEOUT = 5
STATE_KEYS = ("latest_version", "latest_url", "latest_date", "latest_sha", "checked_at",
              "checked_epoch", "error")

_RELEASE_RE = re.compile(r"^##\s*\[?([^\]\s]+)\]?(?:\s*-\s*(\S+))?\s*$")
_SECTION_RE = re.compile(r"^###\s*(.+?)\s*$")
_ITEM_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


def _root():
    """! @brief The repository root: the parent of the modules/ folder (tests monkeypatch this)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_version_file():
    """! @brief The contents of VERSION at the repo root, or "0.0.0-dev" when it is missing or empty."""
    try:
        with open(os.path.join(_root(), "VERSION"), "r", encoding="utf-8") as fh:
            v = fh.read().strip()
    except OSError:
        return FALLBACK_VERSION
    return v or FALLBACK_VERSION


def _git(*args):
    """! @brief Run a git command at the repo root; its stripped stdout, or "" on any failure."""
    try:
        p = subprocess.run(["git", *args], cwd=_root(), capture_output=True, text=True,
                           timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


def git_info():
    """! @brief (short hash, ISO commit date) of HEAD when the root is a git checkout, else ("", "")."""
    if not os.path.isdir(os.path.join(_root(), ".git")):
        return "", ""
    return _git("rev-parse", "--short", "HEAD"), _git("log", "-1", "--format=%cI")


def local_version():
    """! @brief {version, commit, date, channel}: VERSION plus the git hash / date of a checkout."""
    commit, date = git_info()
    return {"version": read_version_file(), "commit": commit, "date": date,
            "channel": "git" if commit else "release"}


def version_tuple(v):
    """! @brief "v1.2.0-dev" -> (1, 2, 0); the leading v and any -suffix are ignored. "" -> ()."""
    m = _VERSION_RE.match(str(v or ""))
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(remote, local):
    """! @brief True when the remote release tag is strictly newer than the local version string."""
    r, l = version_tuple(remote), version_tuple(local)
    if not r or not l:
        return False
    n = max(len(r), len(l))
    return r + (0,) * (n - len(r)) > l + (0,) * (n - len(l))


def parse_changelog(text):
    """! @brief Parse Keep-a-Changelog markdown into [{version, date, sections: {name: [items]}}].
    Releases keep the file's order (Unreleased first by convention); an item that
    continues on an indented line is appended to the previous item.
    """
    releases = []
    cur, section = None, None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        m = _RELEASE_RE.match(line)
        if m and line.startswith("## "):
            cur = {"version": m.group(1), "date": m.group(2) or "", "sections": {}}
            releases.append(cur)
            section = None
            continue
        m = _SECTION_RE.match(line)
        if m and cur is not None:
            section = m.group(1)
            cur["sections"].setdefault(section, [])
            continue
        m = _ITEM_RE.match(line)
        if m and cur is not None and section is not None:
            cur["sections"][section].append(m.group(1))
            continue
        if cur is not None and section is not None and line.startswith("  ") and line.strip() \
                and cur["sections"][section]:
            cur["sections"][section][-1] += " " + line.strip()
    unreleased = [r for r in releases if r["version"].lower() == "unreleased"]
    return unreleased + [r for r in releases if r["version"].lower() != "unreleased"]


def read_changelog():
    """! @brief The raw CHANGELOG.md at the repo root, or "" when missing."""
    try:
        with open(os.path.join(_root(), "CHANGELOG.md"), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _get_json(url):
    """! @brief GET a JSON document from GitHub's API (10 s timeout, User-Agent set).
    @throws urllib.error.HTTPError / URLError / ValueError on failure.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_latest(repo):
    """! @brief Ask GitHub for the latest release of `repo`, else its default branch's head.
    @return {latest_version, latest_url, latest_date, latest_sha, error}; network and
            parse errors land in `error`, nothing is raised.
    """
    out = {"latest_version": "", "latest_url": "", "latest_date": "", "latest_sha": "", "error": ""}
    base = "https://api.github.com/repos/%s" % repo
    try:
        rel = _get_json(base + "/releases/latest")
        out["latest_version"] = str(rel.get("tag_name") or rel.get("name") or "")
        out["latest_url"] = str(rel.get("html_url") or "")
        out["latest_date"] = str(rel.get("published_at") or rel.get("created_at") or "")
        return out
    except urllib.error.HTTPError as e:
        if e.code != 404:
            out["error"] = "releases: HTTP %s" % e.code
            return out
    except Exception as e:
        out["error"] = "releases: %s" % e
        return out
    # no releases: the default branch's latest commit
    try:
        info = _get_json(base)
        branch = str(info.get("default_branch") or "main")
        c = _get_json(base + "/commits/" + branch)
        out["latest_sha"] = str(c.get("sha") or "")
        out["latest_url"] = str(c.get("html_url") or "")
        commit = c.get("commit") or {}
        out["latest_date"] = str(((commit.get("committer") or commit.get("author") or {})
                                  .get("date")) or "")
    except Exception as e:
        out["error"] = "commits: %s" % e
    return out


def register(host):
    """! @brief Wire the version module: settings, state table, routes, info section, service, check thread."""
    log = host.logger
    db = host.db
    lock = threading.Lock()
    git_cache = {}

    host.register_feature(FEATURE, "Version & updates", section="admin", section_label="Admin",
                          default="read")
    host.add_config_key("version_check_enabled", default=True, validate=lambda v: bool(v))
    host.add_config_key("version_check_hours", default=24,
                        validate=lambda v: max(1, min(24 * 30, int(float(v)))))
    host.add_config_key("version_repo", default=DEFAULT_REPO,
                        validate=lambda v: (str(v or "").strip().strip("/") or DEFAULT_REPO))
    host.add_settings_field(key="version_check_enabled", label="Check for updates", kind="toggle",
                            pane="module", help="Ask GitHub for a newer release in the background.")
    host.add_settings_field(key="version_check_hours", label="Check every (hours)", kind="number",
                            pane="module")
    host.add_settings_field(key="version_repo", label="GitHub repository", kind="text",
                            pane="module", help="owner/name of the repository to check.")
    host.add_table("CREATE TABLE IF NOT EXISTS version_state (key TEXT PRIMARY KEY, value TEXT)", kind="state")

    def local_info():
        """! @brief The local version block: VERSION read live, the git hash / date once per process."""
        if "v" not in git_cache:
            git_cache["v"] = git_info()
        commit, date = git_cache["v"]
        return {"version": read_version_file(), "commit": commit, "date": date,
                "channel": "git" if commit else "release"}

    def state_get():
        """! @brief The cached update-check result from version_state as {key: value}."""
        out = {k: "" for k in STATE_KEYS}
        try:
            for r in db().execute("SELECT key, value FROM version_state").fetchall():
                out[r["key"]] = r["value"] or ""
        except Exception as e:
            log.debug("version_state read failed: %s" % e)
        return out

    def state_put(values):
        """! @brief Upsert every (key, value) of `values` into version_state."""
        try:
            d = db()
            d.executemany("INSERT INTO version_state(key, value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          [(k, "" if v is None else str(v)) for k, v in values.items()])
            d.commit()
        except Exception as e:
            log.error("version_state write failed: %s" % e)

    def update_available(info, st):
        """! @brief True when the cached latest release / commit is newer than this install."""
        if st.get("latest_version"):
            return is_newer(st["latest_version"], info["version"])
        if info.get("channel") == "git" and st.get("latest_sha") and info.get("commit"):
            return not st["latest_sha"].startswith(info["commit"])
        return False

    def check_now():
        """! @brief Run the GitHub check and cache its result; returns the new state."""
        repo = str(host.config.get("version_repo") or DEFAULT_REPO)
        with lock:
            res = fetch_latest(repo)
            res["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            res["checked_epoch"] = "%.0f" % time.time()
            state_put(res)
        return state_get()

    def payload():
        """! @brief The /api/version body."""
        info = local_info()
        st = state_get()
        latest = {"version": st["latest_version"], "url": st["latest_url"],
                  "date": st["latest_date"], "sha": st["latest_sha"]}
        return {"success": True, "version": info["version"], "commit": info["commit"],
                "date": info["date"], "channel": info["channel"], "latest": latest,
                "update_available": update_available(info, st),
                "checked_at": st["checked_at"], "error": st["error"],
                "repo": str(host.config.get("version_repo") or DEFAULT_REPO)}

    # -- routes ---------------------------------------------------------------------
    def api_version():
        """! @brief GET /api/version: the local version, the cached latest and whether an update exists."""
        return jsonify(payload())

    def api_changelog():
        """! @brief GET /api/version/changelog: the parsed releases and the raw markdown."""
        raw = read_changelog()
        return jsonify({"success": True, "releases": parse_changelog(raw), "raw": raw})

    def api_check():
        """! @brief POST /api/version/check: run the update check now (admins only)."""
        check_now()
        return jsonify(payload())

    host.add_route("/api/version", api_version, feature=FEATURE)
    host.add_route("/api/version/changelog", api_changelog, feature=FEATURE)
    host.add_route("/api/version/check", api_check, methods=["POST"], feature=FEATURE,
                   level="write", action="version_check", admin=True)

    # -- Settings -> Info -------------------------------------------------------------
    def _info_section():
        """! @brief The "About" section: version, commit, build date, latest release, last check."""
        p = payload()
        rows = [{"label": "Version", "value": p["version"]}]
        if p["commit"]:
            rows.append({"label": "Commit", "value": p["commit"]})
        if p["date"]:
            rows.append({"label": "Built", "value": p["date"]})
        latest = p["latest"]
        if latest["version"] or latest["sha"]:
            what = latest["version"] or latest["sha"][:7]
            if latest["date"]:
                what += " (%s)" % latest["date"][:10]
            what += " - update available" if p["update_available"] else " - up to date"
            row = {"label": "Latest release" if latest["version"] else "Latest commit", "value": what}
            if latest["url"]:
                row["url"] = latest["url"]
            rows.append(row)
        elif p["error"]:
            rows.append({"label": "Latest release", "value": "check failed: %s" % p["error"]})
        else:
            rows.append({"label": "Latest release", "value": "not checked yet"})
        rows.append({"label": "Last checked", "value": p["checked_at"] or "never"})
        rows.append({"label": "Changelog", "value": "%d releases" % len(parse_changelog(read_changelog()))})
        return {"id": "about", "title": "About", "rows": rows}
    host.on("info.sections", _info_section)

    # -- the `about` service --------------------------------------------------------
    info0 = local_info()
    host.provide_service("about", {"version": info0["version"], "commit": info0["commit"],
                                   "date": info0["date"], "channel": info0["channel"]})

    # -- background check -------------------------------------------------------------
    def _due():
        """! @brief True when the toggle is on and the last check is older than version_check_hours."""
        if not host.config.get("version_check_enabled", True):
            return False
        try:
            hours = max(1.0, float(host.config.get("version_check_hours") or 24))
        except (TypeError, ValueError):
            hours = 24.0
        last = state_get().get("checked_epoch") or ""
        try:
            return time.time() - float(last) >= hours * 3600
        except ValueError:
            return True

    def _loop():
        """! @brief Check at startup, then whenever a check is due; a toggle flip is picked up within a minute."""
        first = True
        while True:
            try:
                if (first and host.config.get("version_check_enabled", True)) or _due():
                    check_now()
            except Exception as e:
                log.error("version check failed: %s" % e)
            first = False
            time.sleep(60)

    def _start():
        """! @brief Start the daemon check thread once the server is up."""
        threading.Thread(target=_loop, name="version-check", daemon=True).start()
    host.on_startup(_start)

    host.add_asset("version.js")
    log.info("version module registered (%s %s)" % (info0["version"], info0["commit"] or "release"))
