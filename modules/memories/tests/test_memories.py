"""! @file
@brief Memories module: years-ago groups, window, current-year exclusion, scope, min_files."""
from datetime import datetime, timezone

import pytest

FOLDER = "mem_test"
OTHER = "mem_other"
COLS = ("d_actual", "d_original", "d_capture", "d_digitized", "d_modified")


def _set_date(host, fn, iso, col="d_original"):
    """! @brief Put a file into exactly one date bucket (or none)."""
    db = host.db()
    db.execute("UPDATE files SET " + ", ".join(f"{c}=NULL, {c}_epoch=NULL" for c in COLS)
               + " WHERE rel_path=?", (fn,))
    if iso:
        ep = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()
        db.execute(f"UPDATE files SET {col}=?, {col}_epoch=? WHERE rel_path=?", (iso[:10], ep, fn))
    db.commit()


@pytest.fixture
def lib(host, upload):
    plan = {
        "a.png": ("2021-06-15T10:00:00", FOLDER),   # 3 years before 2024-06-15
        "b.png": ("2021-06-15T12:00:00", FOLDER),
        "c.png": ("2022-06-16T09:00:00", FOLDER),   # adjacent day, only with window=1
        "d.png": ("2023-06-15T08:00:00", FOLDER),   # 1 year ago
        "e.png": ("2024-06-15T18:00:00", FOLDER),   # current year: excluded
        "f.png": (None, FOLDER),                    # undated
        "g.png": ("2020-06-15T08:00:00", OTHER),    # other folder
        "h.png": ("2019-01-01T08:00:00", FOLDER),   # year wrap: 2020-12-31 window=1
    }
    out = {}
    for i, (name, (iso, folder)) in enumerate(plan.items()):
        fn = upload(seed=1300 + i, name=name, folder=folder)
        out[name] = fn
        _set_date(host, fn, iso)
    _set_date(host, out["d.png"], "2023-06-15T08:00:00", col="d_capture")
    return out


@pytest.fixture
def defaults(host):
    """! @brief Reset the module settings before and after a test."""
    keep = {k: host.config.get(k) for k in ("memories_window_days", "memories_min_files", "memories_show_on_start")}
    host.config["memories_window_days"] = 0
    host.config["memories_min_files"] = 1
    host.config["memories_show_on_start"] = True
    yield host
    host.config.update(keep)


def _mem(client, **q):
    q.setdefault("folder", FOLDER)
    j = client.get("/api/memories", query_string=q).get_json()
    assert j["success"], j
    return j


def test_groups_and_counts(client, lib, defaults):
    j = _mem(client, date="2024-06-15")
    assert j["date"] == "2024-06-15" and j["window"] == 0
    assert [(m["years_ago"], m["year"], m["count"]) for m in j["memories"]] == [(1, 2023, 1), (3, 2021, 2)]
    one, three = j["memories"]
    assert one["title"] == "1 year ago" and one["date"] == "2023-06-15"
    assert three["title"] == "3 years ago" and three["date"] == "2021-06-15"
    assert [f["filename"] for f in three["files"]] == [lib["b.png"], lib["a.png"]]   # newest first
    assert three["files"][0]["date"] == "2021-06-15" and three["files"][0]["kind"] == "image"
    # the current year never appears, nor undated files
    names = {f["filename"] for m in j["memories"] for f in m["files"]}
    assert lib["e.png"] not in names and lib["f.png"] not in names


def test_window_includes_adjacent_days(client, lib, defaults):
    j = _mem(client, date="2024-06-15", window=1)
    assert [(m["years_ago"], m["count"]) for m in j["memories"]] == [(1, 1), (2, 1), (3, 2)]
    assert j["memories"][1]["files"][0]["filename"] == lib["c.png"]
    # the setting supplies the default window
    defaults.config["memories_window_days"] = 1
    assert [m["year"] for m in _mem(client, date="2024-06-15")["memories"]] == [2023, 2022, 2021]


def test_window_crosses_year_boundary(client, lib, defaults):
    assert _mem(client, date="2020-12-31")["memories"] == []
    j = _mem(client, date="2020-12-31", window=1)
    assert [(m["years_ago"], m["year"], m["count"]) for m in j["memories"]] == [(1, 2019, 1)]
    assert j["memories"][0]["files"][0]["filename"] == lib["h.png"]


def test_folder_scope_and_limit(client, lib, defaults):
    assert lib["g.png"] not in {f["filename"] for m in _mem(client, date="2024-06-15")["memories"] for f in m["files"]}
    j = _mem(client, date="2024-06-15", folder=OTHER)
    assert [(m["years_ago"], m["count"]) for m in j["memories"]] == [(4, 1)]
    stem = lib["a.png"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    q = _mem(client, date="2024-06-15", q=stem)
    assert [(m["year"], m["count"]) for m in q["memories"]] == [(2021, 1)]
    lim = _mem(client, date="2024-06-15", limit=1)
    three = [m for m in lim["memories"] if m["year"] == 2021][0]
    assert three["count"] == 2 and len(three["files"]) == 1 and three["files"][0]["filename"] == lib["b.png"]


def test_min_files(client, lib, defaults):
    defaults.config["memories_min_files"] = 2
    j = _mem(client, date="2024-06-15")
    assert [m["year"] for m in j["memories"]] == [2021]
    assert [m["year"] for m in _mem(client, date="2024-06-15", min_files=1)["memories"]] == [2023, 2021]


def test_years_config_and_defaults(client, lib, defaults):
    years = client.get("/api/memories/years", query_string={"folder": FOLDER}).get_json()
    assert years["success"] and years["years"] == [2024, 2023, 2022, 2021, 2019]
    cfg = client.get("/api/memories/config").get_json()
    assert cfg == {"success": True, "window": 0, "min_files": 1, "show_on_start": True}
    today = _mem(client)
    assert today["date"] == datetime.now().date().isoformat() and today["show_on_start"] is True


def test_bad_date(client):
    assert client.get("/api/memories", query_string={"date": "2024-6-1"}).status_code == 400
    assert client.get("/api/memories", query_string={"date": "2024-02-30"}).status_code == 400


def test_contributes_assets(client):
    assets = client.get("/api/module_assets").get_json()["assets"]
    urls = [a["url"] for a in assets if a["module_id"] == "memories"]
    assert any(u.endswith("/memories.js") for u in urls) and any(u.endswith("/memories.css") for u in urls)
