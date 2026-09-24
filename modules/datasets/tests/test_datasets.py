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
    for bad in ("https://x.org/a.zip", "dataset:", "dataset:hf:onlyname", "dataset:ftp://x"):
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
            add_config_key=lambda *a, **k: None, add_settings_field=lambda **k: None)
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