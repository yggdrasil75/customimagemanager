"""! @file
@brief /api/background/progress: every background sweep reports, and each one's
progress fn counts without error (a broken query would hide its bar).
"""


def test_background_progress_lists_every_sweep(app, client, ungated):
    sweeps = app.module_host.background_sweeps
    j = client.get("/api/background/progress").get_json()
    assert j["success"], j
    rows = {r["cap"]: r for r in j["sweeps"]}
    assert set(rows) == set(sweeps)
    db = app._db()
    for cap, sw in sweeps.items():
        r = rows[cap]
        assert {"enabled", "failed", "session_done"} <= set(r), r
        if sw.get("progress"):
            got = sw["progress"](db)                      # no SQL error
            assert 0 <= got["done"] <= got["total"], (cap, got)