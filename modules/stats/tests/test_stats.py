"""! @file
@brief Tests for the stats module: thread-manager counters, pause / resume,
server statistics and the slow-jobs listing.
"""
import time


def _wait(pred, timeout=5.0):
    """! @brief Poll `pred` until true or the timeout; return its last value."""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _sources(client):
    return client.get("/api/stats/jobs").get_json()["sources"]


def test_jobs_counters_and_history(client, host):
    tm = host.thread_manager
    jobs = ["ok", "bad"]

    def claim():
        return jobs.pop() if jobs else None

    def handle(j):
        time.sleep(0.05)
        if j == "bad":
            raise RuntimeError("test failure")

    tm.register_source("stats_test_a", claim, handle, key_of=lambda j: "stats_test_a:" + j)
    tm.wake()
    assert _wait(lambda: (_sources(client).get("stats_test_a") or {}).get("done") == 1
                 and _sources(client)["stats_test_a"]["failed"] == 1)
    j = client.get("/api/stats/jobs").get_json()
    assert j["success"]
    s = j["sources"]["stats_test_a"]
    assert s["started"] == 2 and s["done"] == 1 and s["failed"] == 1 and s["running"] == 0
    assert s["last_seconds"] >= 0.04 and s["max_seconds"] >= 0.04 and s["avg_seconds"] >= 0.04
    assert "RuntimeError" in s["last_error"]
    assert s["paused"] is False and s["running_jobs"] == []
    hist = [h for h in j["history"] if h["source"] == "stats_test_a"]
    assert len(hist) == 2
    assert {h["ok"] for h in hist} == {True, False}
    assert all(h["key"].startswith("stats_test_a:") for h in hist)
    assert "status" in j and "max_slots" in j["status"]
    assert isinstance(j["upload_queue"], dict) and "busy" in j["pressure"]


def test_pause_resume(client, host):
    tm = host.thread_manager
    calls = [0]

    def claim():
        calls[0] += 1
        return None

    tm.register_source("stats_test_p", claim, lambda j: None)
    r = client.post("/api/stats/jobs/pause", json={"source": "stats_test_p"}).get_json()
    assert r["success"] and "stats_test_p" in r["paused"]
    assert _sources(client)["stats_test_p"]["paused"] is True
    time.sleep(0.3)  # a pass already in flight may finish
    before = calls[0]
    tm.wake()
    time.sleep(1.5)
    assert calls[0] == before, "a paused source was still asked for work"
    r = client.post("/api/stats/jobs/resume", json={"source": "stats_test_p"}).get_json()
    assert r["success"] and "stats_test_p" not in r["paused"]
    assert _wait(lambda: calls[0] > before, timeout=3.0)
    assert _sources(client)["stats_test_p"]["paused"] is False
    # unknown source / missing body
    assert client.post("/api/stats/jobs/pause", json={"source": "no_such_source"}).status_code == 404
    assert client.post("/api/stats/jobs/pause", json={}).status_code == 400


def test_server_stats(client, host, upload):
    before = client.get("/api/stats/server").get_json()["library"]["files"]
    upload("stats_a.png", seed=11)
    upload("stats_b.png", seed=12)
    d = client.get("/api/stats/server").get_json()
    assert d["success"]
    assert d["library"]["files"] == before + 2
    assert d["library"]["images"] >= 2
    assert d["disk"]["total"] > 0 and d["disk"]["free"] >= 0
    assert d["db"]["bytes"] > 0 and d["db"]["path"]
    assert d["uptime_seconds"] >= 0 and d["python"] and d["platform"]
    assert "enabled" in d["modules"] and "loaded" in d["models"]
    assert "count" in d["users"]
    # the cached walk measures what was uploaded
    svc = host.get_service("stats")
    v = svc["measure_library"]()
    assert v["files"] >= 2 and v["bytes"] > 0
    lib = client.get("/api/stats/server").get_json()["library"]
    assert lib["source"] == "walk" and lib["bytes"] == v["bytes"]


def test_slow_sorted(client, host):
    tm = host.thread_manager
    jobs = [0.02, 0.08, 0.04]

    def claim():
        return jobs.pop() if jobs else None

    tm.register_source("stats_test_s", claim, lambda j: time.sleep(j))
    tm.wake()
    assert _wait(lambda: (_sources(client).get("stats_test_s") or {}).get("done") == 3)
    d = client.get("/api/stats/slow?n=5").get_json()
    assert d["success"]
    secs = [j["seconds"] for j in d["jobs"]]
    assert secs == sorted(secs, reverse=True) and len(secs) <= 5
    avgs = [s["avg_seconds"] or 0 for s in d["sources"]]
    assert avgs == sorted(avgs, reverse=True)
    assert any(s["source"] == "stats_test_s" for s in d["sources"])


def test_info_section(client):
    secs = client.get("/api/info").get_json()["sections"]
    srv = next((s for s in secs if s["id"] == "server"), None)
    assert srv is not None
    labels = {r["label"] for r in srv["rows"]}
    assert {"Files", "Uptime"} <= labels
