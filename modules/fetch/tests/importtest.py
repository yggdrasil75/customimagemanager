"""! @file
@brief Shared test helpers for importer fetchers (Immich, Google Takeout, Apple):
drive a source's run the way the UI does, synchronously, through the real
fetch job -> ledger -> upload queue -> ingest -> reconcile path."""
import pytest


def drain_uploads(app):
    while True:
        job = app._claim_upload_job()
        if not job:
            break
        app._handle_upload_job(job)


def run_source(client, app, fetcher, sid, **body):
    f = app.module_host.get_service("fetch")
    r = client.post(f"/api/import/{fetcher}/run", json={"id": sid, **body})
    assert r.status_code == 200, r.get_json()
    qid = app._db().execute("SELECT MAX(id) FROM fetch_queue WHERE fetcher=?", (fetcher,)).fetchone()[0]
    row = f.run(qid)
    drain_uploads(app)
    f.reconcile()
    return row


def ledger(app, fetcher, name):
    return app._db().execute("SELECT i.rel_path, i.status, i.error, f.d_original FROM fetch_items i LEFT JOIN files f "
                             "ON f.rel_path=i.rel_path WHERE i.fetcher=? AND i.name=?", (fetcher, name)).fetchone()


@pytest.fixture
def imports(app, client, tmp_path):
    """! @brief An empty import folder, and a clean slate for fetch/import tables after."""
    root = tmp_path / "imports"; root.mkdir()
    old = app.state.get("import_root")
    app.state["import_root"] = str(root)
    yield root
    app.state["import_root"] = old or "imports"
    db = app._db()
    for r in db.execute("SELECT rel_path FROM fetch_items WHERE rel_path<>''").fetchall():
        client.post("/api/delete", json={"filename": r["rel_path"]})
    for t in ("fetch_items", "fetch_queue", "fetch_watch", "import_sources", "upload_queue"):
        db.execute(f"DELETE FROM {t}")
    db.commit()
