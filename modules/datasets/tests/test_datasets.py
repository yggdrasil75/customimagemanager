"""datasets: target parsing, archive + parquet normalisation, labels.csv, and
the fetcher end to end against a local HTTP server (no network).
    python -m pytest -q modules/datasets/tests/test_datasets.py
"""
import csv
import functools
import http.server
import io
import logging
import os
import threading
import types
import zipfile

import pytest

from modules.datasets import ds
from modules.datasets import module as dsm

JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60


def _drain(gen):
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def _labels(path):
    with open(path, newline="") as f:
        return {r["name"]: float(r["score"]) for r in csv.DictReader(f)}


def test_parse_target():
    s = ds.parse_target("dataset:hf:org/koniq split=train score=MOS")
    assert s == {"kind": "hf", "src": "org/koniq", "name": "koniq", "split": "train", "score": "MOS"}
    assert ds.parse_target("dataset:https://huggingface.co/datasets/a/b/tree/main")["src"] == "a/b"
    u = ds.parse_target("dataset:https://x.org/dl/koniq10k_1024x768.zip name=koniq")
    assert (u["kind"], u["name"], ds.host_of(u)) == ("url", "koniq", "x.org")
    assert ds.parse_target("dataset:https://x.org/a/imgs.tar.gz")["name"] == "imgs"
    assert ds.parse_target("dataset:https://www.kaggle.com/datasets/own/set?x=1")["src"] == "own/set"
    z = ds.parse_target("dataset:https://zenodo.org/records/12345 name=zz")
    assert (z["kind"], z["src"], z["name"], ds.host_of(z)) == ("zenodo", "12345", "zz", "zenodo.org")
    p = ds.parse_target("dataset:pyiqa:koniq10k")
    assert (p["name"], ds.host_of(p)) == ("koniq10k", "huggingface.co")
    for bad in ("https://x.org/a.zip", "dataset:", "dataset:hf:onlyname", "dataset:ftp://x",
                "dataset:pyiqa:nope", "dataset:zenodo:abc", "dataset:ultralytics:nope"):
        with pytest.raises(ds.DatasetError):
            ds.parse_target(bad)


def test_zip_with_scores_csv(tmp_path):
    z = tmp_path / "set.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("set/imgs/1.jpg", JPG)
        zf.writestr("set/imgs/2.jpg", JPG)
        zf.writestr("set/imgs/3.jpg", JPG)
        zf.writestr("set/scores.csv", "image_name,c1,c2,MOS\n1.jpg,9,9,1.0\n2.jpg,9,9,3.0\n3,9,9,5.0\n")
    labels = _drain(ds.normalise(str(tmp_path), {}))
    assert not z.exists() and (tmp_path / "set" / "imgs" / "2.jpg").exists()
    assert labels == str(tmp_path / "labels.csv")
    assert _labels(labels) == {"1.jpg": 0.0, "2.jpg": 0.5, "3.jpg": 1.0}


def test_unlabelled_and_ava(tmp_path):
    (tmp_path / "a.jpg").write_bytes(JPG)
    assert _drain(ds.normalise(str(tmp_path), {})) is None          # dedup-only folder
    sub = tmp_path / "AVA_dataset"
    sub.mkdir()
    (sub / "AVA.txt").write_text("1 a 0 0 0 0 0 0 0 0 0 1 0 0 0\n")
    assert _drain(ds.normalise(str(tmp_path), {})) == str(sub / "AVA.txt")


def test_parquet(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    t = pa.table({"image": [{"bytes": JPG, "path": "x.jpg"}, {"bytes": PNG, "path": None}],
                  "Score": [2.0, 4.0]})
    (tmp_path / "data").mkdir()
    pq.write_table(t, tmp_path / "data" / "train-0.parquet")
    labels = _drain(ds.normalise(str(tmp_path), {}))
    assert not (tmp_path / "data" / "train-0.parquet").exists()
    assert (tmp_path / "images" / "x.jpg").read_bytes() == JPG
    assert (tmp_path / "images" / "train-0_0000002.png").read_bytes() == PNG
    assert _labels(labels) == {"x.jpg": 0.0, "train-0_0000002.png": 1.0}
    assert _labels(_drain(ds.normalise(str(tmp_path), {}))) == _labels(labels)   # rerun is stable


def test_fetcher_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    srv_dir = tmp_path / "srv"
    srv_dir.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.jpg", JPG)
        zf.writestr("b.jpg", JPG)
        zf.writestr("mos.csv", "file,mos\na.jpg,1\nb.jpg,2\n")
    (srv_dir / "pack.zip").write_bytes(buf.getvalue())
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(srv_dir))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        reg = {}
        media = tmp_path / "media"
        media.mkdir()
        host = types.SimpleNamespace(
            config={"dedup_train_folders": "/old"}, media_dir=str(media),
            logger=logging.getLogger("t"), save_config=lambda: None,
            get_service=lambda n: types.SimpleNamespace(register=lambda f: reg.update(f)),
            add_config_key=lambda *a, **k: None, add_settings_field=lambda **k: None,
            add_settings_tab=lambda *a, **k: None, add_asset=lambda *a: None, add_route=lambda *a, **k: None)
        dsm.register(host)
        target = f"dataset:http://127.0.0.1:{srv.server_address[1]}/pack.zip name=my set"
        assert reg["handles"](target) and not reg["handles"]("https://x.org/a")
        list(reg["fetch"](target, str(tmp_path), on_file=None))
        dest = str(media / ".datasets" / "my")                    # name= stops at the first space
        assert sorted(os.listdir(dest)) == [".fetched_pack.zip.done", "a.jpg", "b.jpg", "labels.csv", "mos.csv"]
        assert host.config["dedup_train_folders"] == f"/old\n{dest}"
        assert host.config["iqa_train_datasets"] == f"{dest} {dest}/labels.csv"
        list(reg["fetch"](target, str(tmp_path), on_file=None))  # rerun: no re-download, no duplicate lines
        assert host.config["dedup_train_folders"] == f"/old\n{dest}"
    finally:
        srv.shutdown()


def test_auth_headers():
    c = {"hf": "tok", "kaggle_user": "u", "kaggle_key": "k"}
    assert ds._auth_header("https://huggingface.co/api/x", c) == "Bearer tok"
    assert ds._auth_header("https://www.kaggle.com/api/v1/x", c) == "Basic dTpr"
    assert ds._auth_header("https://zenodo.org/api/records/1", c) is None
    assert ds._auth_header("https://www.kaggle.com/api/v1/x", {"hf": "tok"}) is None


def test_zenodo_files(monkeypatch):
    rec = {"files": [{"key": "a.zip", "size": 3, "links": {"self": "https://z/a.zip/content"}}]}
    monkeypatch.setattr(ds, "_json", lambda url, creds=None: (rec, {}))
    assert ds.zenodo_files("1") == [("a.zip", "https://z/a.zip/content", 3)]
    rec = {"files": {"entries": {"b.csv": {"key": "b.csv", "links": {"content": "https://z/b"}}}}}
    assert ds.zenodo_files("1") == [("b.csv", "https://z/b", None)]


def test_pyiqa_labels_fr_lower_better(tmp_path):
    root = tmp_path / "live"
    for sub in ("jp2k", "wn"):
        (root / "LIVEIQA_release2" / sub).mkdir(parents=True)
        (root / "LIVEIQA_release2" / sub / "img1.bmp").write_bytes(JPG)   # same basename, two folders
    meta = tmp_path / "meta.txt"
    meta.write_text("ref_name,dist_name,dmos\nrefs/a.bmp,jp2k/img1.bmp,1\nrefs/a.bmp,wn/img1.bmp,100\n"
                    "refs/a.bmp,wn/missing.bmp,50\n")
    lab = _labels(ds.pyiqa_labels(str(root), "live", str(meta)))
    assert lab == {"LIVEIQA_release2/jp2k/img1.bmp": 1.0, "LIVEIQA_release2/wn/img1.bmp": 0.0}


def test_pyiqa_labels_out_of_range_falls_back(tmp_path):
    (tmp_path / "PIPAL" / "Dist_Imgs").mkdir(parents=True)
    for n in ("a.png", "b.png"):
        (tmp_path / "PIPAL" / "Dist_Imgs" / n).write_bytes(PNG)
    meta = tmp_path / "m.txt"
    meta.write_text("ref,dist,elo\nr.png,a.png,1000\nr.png,b.png,1500\n")      # published range says 0..1
    assert _labels(ds.pyiqa_labels(str(tmp_path), "pipal", str(meta))) == {
        "PIPAL/Dist_Imgs/a.png": 0.0, "PIPAL/Dist_Imgs/b.png": 1.0}


def test_iqa_train_reads_relative_label_paths(tmp_path):
    bd = pytest.importorskip("modules.iqa_train.build")
    for sub in ("jp2k", "wn"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "img1.bmp").write_bytes(JPG)
    lf = tmp_path / "labels.csv"
    lf.write_text("name,score\njp2k/img1.bmp,0.2\nwn/img1.bmp,0.9\n")
    got = dict(bd.read_labels(str(tmp_path), str(lf)))
    assert got == {str(tmp_path / "jp2k" / "img1.bmp"): 0.2, str(tmp_path / "wn" / "img1.bmp"): 0.9}


def test_ultralytics_zoo_and_script(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    cfgd = tmp_path / "cfg"
    cfgd.mkdir()
    (cfgd / "tiny8.yaml").write_text("# Ultralytics AGPL-3.0 License\n# Documentation: x\n# Tiny8 test set\npath: tiny8 # root\n"
                                     "download: https://github.com/ultralytics/assets/releases/download/v0.0.0/tiny8.zip\n")
    (cfgd / "scripted.yaml").write_text(
        "path: scr\nnames: {0: a}\ndownload: |\n  import zipfile\n  d = yaml['path']\n"
        "  (d / 'images').mkdir(parents=True, exist_ok=True)\n"
        "  (d / 'images' / 'x.jpg').write_bytes(b'\\xff\\xd8')\n"
        "  zipfile.ZipFile(d.parent / 'left.zip', 'w').close()\n  zipfile.ZipFile(d / 'in.zip', 'w').close()\n")
    (cfgd / "bashy.yaml").write_text("path: b\ndownload: ultralytics/data/scripts/get_x.sh\n")
    monkeypatch.setattr(ds, "_ultra_dir", lambda: str(cfgd))
    z = ds.ultralytics_zoo()
    assert sorted(z) == ["scripted", "tiny8"] and z["tiny8"]["label"] == "Tiny8 test set"
    items = {it["target"]: it for it in next(q for q in ds.zoo() if q["id"] == "ultralytics")["items"]}
    assert items["dataset:ultralytics:tiny8"]["name"] == "tiny8"
    spec = ds.parse_target("dataset:ultralytics:scripted")
    assert ds.host_of(spec) == "ultralytics" and spec["name"] == "scr"
    dest = tmp_path / "ds" / "scr"
    assert _drain(ds.fetch(spec, str(dest))) is None
    assert (dest / "images" / "x.jpg").exists()
    assert not (tmp_path / "ds" / "left.zip").exists() and not (dest / "in.zip").exists()
    (dest / "images" / "x.jpg").unlink()
    _drain(ds.fetch(spec, str(dest)))                      # marker: the script does not run twice
    assert not (dest / "images" / "x.jpg").exists()