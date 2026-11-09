"""! @file
@brief Model share: devices get a YOLO box model by name or by a class it boxes.
    ./run_tests.sh modules/model_share
"""
import json
import os
import shutil

import pytest


@pytest.fixture
def models(app):
    """! @brief Two trained runs and one custom .pt with known classes (no torch needed:
    a run's classes come from cim_run.json, a loose .pt's from its .classes.json)."""
    root = os.path.abspath(app.module_host.core.models_dir)
    made = []

    def run(name, classes, data):
        d = os.path.join(root, "runs", "detect", name)
        os.makedirs(os.path.join(d, "weights"), exist_ok=True)
        with open(os.path.join(d, "weights", "best.pt"), "wb") as f:
            f.write(data)
        with open(os.path.join(d, "cim_run.json"), "w") as f:
            json.dump({"set": name, "run": name, "classes": classes, "base_model": "yolo11n.pt"}, f)
        made.append(d)

    run("set_ms_line_train_1", ["blue tube", "red cap", "label"], b"RUN1")
    run("set_ms_tube_train_2", ["blue_tube"], b"RUN2")
    loose = os.path.join(root, "ms_widgets.pt")
    with open(loose, "wb") as f:
        f.write(b"LOOSE")
    with open(loose + ".classes.json", "w") as f:
        json.dump({"mtime": os.path.getmtime(loose), "classes": ["widget", "blue tube"]}, f)
    made += [loose, loose + ".classes.json"]
    yield
    for p in made:
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)


def test_list_and_find_by_class_prefers_the_specialised_trained_run(client, ungated, models):
    j = client.get("/api/model_share/yolo").get_json()
    names = {m["name"]: m for m in j["models"]}
    assert {"set_ms_line_train_1", "set_ms_tube_train_2", "ms_widgets"} <= set(names)
    assert names["ms_widgets"]["classes"] == ["widget", "blue tube"]
    # "Blue-Tube", "blue_tube" and "blue tube" are the same class
    f = client.get("/api/model_share/yolo/find", query_string={"class": "Blue-Tube"}).get_json()
    assert f["success"] and f["model"]["name"] == "set_ms_tube_train_2", f
    only = client.get("/api/model_share/yolo", query_string={"class": "widget"}).get_json()["models"]
    assert [m["name"] for m in only] == ["ms_widgets"]


def test_find_and_download_by_name(client, ungated, models):
    for q in ({"name": "ms_widgets"}, {"name": "ms_widgets.pt"}):
        r = client.get("/api/model_share/yolo/download", query_string=q)
        assert r.status_code == 200 and r.get_data() == b"LOOSE", q
    r = client.get("/api/model_share/yolo/download", query_string={"name": "set_ms_line_train_1"})
    assert r.get_data() == b"RUN1" and "set_ms_line_train_1.pt" in r.headers["Content-Disposition"]
    assert json.loads(r.headers["X-Model-Classes"]) == ["blue tube", "red cap", "label"]
    # name + class: the named model, only if it boxes that class
    assert client.get("/api/model_share/yolo/find",
                      query_string={"name": "ms_widgets", "class": "red cap"}).status_code == 404


def test_unknown_class_and_bad_format(client, ungated, models):
    r = client.get("/api/model_share/yolo/find", query_string={"class": "giraffe"})
    assert r.status_code == 404 and "known classes" in r.get_json()["error"]
    assert client.get("/api/model_share/yolo/download",
                      query_string={"class": "widget", "format": "exe"}).status_code == 400
    assert client.get("/api/model_share/yolo/find").status_code == 400


def test_cim_upload_get_model_and_list(app, ungated, models, tmp_path, capsys):
    """! @brief The uploader's --get-model / --list-models against a live server."""
    import threading
    from werkzeug.serving import make_server
    import upload
    srv = make_server("127.0.0.1", 0, app.app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = "http://127.0.0.1:%d" % srv.server_port
    try:
        out = tmp_path / "tube.pt"
        assert upload.model_command(url, "", "", True, "blue tube", False, "pt", str(out), False) == 0
        assert out.read_bytes() == b"RUN2"
        assert upload.model_command(url, "", "", True, "ms_widgets", True, "pt", str(tmp_path / "w.pt"), False) == 0
        assert (tmp_path / "w.pt").read_bytes() == b"LOOSE"
        assert upload.model_command(url, "", "", True, "giraffe", False, "pt", str(tmp_path / "g.pt"), False) == 1
        capsys.readouterr()
        assert upload.model_command(url, "", "", True, None, False, "pt", "", True) == 0
        assert "set_ms_tube_train_2" in capsys.readouterr().out
    finally:
        srv.shutdown()