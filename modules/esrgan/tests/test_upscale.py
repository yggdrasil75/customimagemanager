"""! @file
@brief Upscale module: the ESRGAN checkpoint reader on small random nets in every
layout, the tiled runner, and the jobs end to end (copy and replace) with a tiny
custom model, so nothing downloads. The test app starts no ingest workers: the
copy job's queue row is processed here the way the worker does.
"""
import io
import json
import os

import numpy as np
import pytest
import torch

import model_registry
from cimtest import media_path, signed_in_as
from modules.upscale import esrgan_arch, jobs as jobs_mod, upscaler
from modules.upscale import module as um


def _rrdb(nb=1, nf=8, gc=4, n_up=2, unshuffle=1):
    torch.manual_seed(0)
    return esrgan_arch.RRDBNet(nf=nf, nb=nb, gc=gc, n_up=n_up, unshuffle=unshuffle)


def _to_old_layout(sd, nb, n_up):
    """! @brief BasicSR keys -> original ESRGAN "model.N" keys."""
    out = {}
    hr, last = 3 * n_up + 2, 3 * n_up + 4
    for k, v in sd.items():
        p = k.split(".")
        if p[0] == "conv_first":
            nk = "model.0." + p[-1]
        elif p[0] == "body":
            nk = "model.1.sub.%s.RDB%s.conv%s.0.%s" % (p[1], p[2][3], p[3][4], p[4])
        elif p[0] == "conv_body":
            nk = "model.1.sub.%d.%s" % (nb, p[-1])
        elif p[0].startswith("conv_up"):
            nk = "model.%d.%s" % (3 * int(p[0][7:]), p[-1])
        elif p[0] == "conv_hr":
            nk = "model.%d.%s" % (hr, p[-1])
        else:
            nk = "model.%d.%s" % (last, p[-1])
        out[nk] = v
    return out


def _pic(h=20, w=28, seed=1):
    return (np.random.default_rng(seed).random((h, w, 3)) * 255).astype(np.uint8)


@pytest.fixture
def tiny_model(tmp_path):
    """! @brief A compact x2 net saved as a Real-ESRGAN style checkpoint."""
    torch.manual_seed(0)
    net = esrgan_arch.SRVGGNetCompact(nf=8, n_conv=2, upscale=2)
    p = tmp_path / "tiny_x2.pth"
    torch.save({"params": net.state_dict()}, str(p))
    return str(p)


# -- checkpoints ---------------------------------------------------------------

@pytest.mark.parametrize("n_up", [0, 1, 2, 3])
def test_old_esrgan_layout_matches_new(tmp_path, n_up):
    net = _rrdb(nb=2, n_up=n_up)
    sd = {k: v for k, v in net.state_dict().items()}
    new_p, old_p = tmp_path / "new.pth", tmp_path / "old.pth"
    torch.save({"params_ema": sd}, str(new_p))
    torch.save(_to_old_layout(sd, 2, n_up), str(old_p))
    a, b = upscaler.load_model(str(new_p), "cpu"), upscaler.load_model(str(old_p), "cpu")
    assert (a.arch, b.arch) == ("RRDBNet", "ESRGAN") and a.scale == b.scale == 2 ** n_up
    img = _pic()
    assert np.array_equal(a.run(img, tile=0), b.run(img, tile=0))


@pytest.mark.parametrize("unshuffle,scale", [(2, 2), (4, 1)])
def test_realesrgan_unshuffle_variants(tmp_path, unshuffle, scale):
    p = tmp_path / "m.pth"
    torch.save(_rrdb(unshuffle=unshuffle).state_dict(), str(p))
    u = upscaler.load_model(str(p), "cpu")
    assert u.scale == scale
    out = u.run(_pic(21, 27), tile=0)          # odd sizes get padded for the unshuffle
    assert out.shape == (21 * scale, 27 * scale, 3)


def test_compact_and_module_prefix(tmp_path, tiny_model):
    sd = esrgan_arch.read_state_dict(tiny_model)
    p = tmp_path / "dp.pth"
    torch.save({"state_dict": {"module." + k: v for k, v in sd.items()}}, str(p))
    u = upscaler.load_model(str(p), "cpu")
    assert u.arch == "SRVGGNetCompact" and u.scale == 2


def test_safetensors(tmp_path):
    st = pytest.importorskip("safetensors.torch")
    p = tmp_path / "m.safetensors"
    st.save_file(_rrdb().state_dict(), str(p))
    assert upscaler.load_model(str(p), "cpu").scale == 4


def test_unknown_checkpoint_is_refused(tmp_path):
    p = tmp_path / "x.pth"
    torch.save({"encoder.weight": torch.zeros(3, 3)}, str(p))
    if upscaler.HAVE_SPANDREL:
        pytest.skip("spandrel installed: it gets the last word")
    with pytest.raises(esrgan_arch.UnsupportedModel):
        upscaler.load_model(str(p), "cpu")


# -- running -------------------------------------------------------------------

def test_tiles_cover_the_picture_and_channels_survive(tiny_model):
    u = upscaler.load_model(tiny_model, "cpu")
    img = _pic(50, 70)
    whole, tiled = u.run(img, tile=0), u.run(img, tile=16)
    assert whole.shape == tiled.shape == (100, 140, 3)
    assert np.abs(whole.astype(int) - tiled).mean() < 2
    seen = []
    u.run(img, tile=32, progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (6, 6)
    rgba = np.dstack([img, np.full(img.shape[:2], 77, np.uint8)])
    out = u.run(rgba, tile=0)
    assert out.shape == (100, 140, 4) and int(out[..., 3].mean()) == 77
    assert u.run(img[:, :, 0], tile=0).shape == (100, 140)


def test_factor_helpers():
    assert jobs_mod.plan_passes(4, 4) == 1 and jobs_mod.plan_passes(4, 2) == 1
    assert jobs_mod.plan_passes(2, 8) == 3 and jobs_mod.plan_passes(4, 8) == 2
    assert um.parse_factor("") == 0 and um.parse_factor("3") == 3
    with pytest.raises(ValueError):
        um.parse_factor("0.5")


# -- the module in the app -----------------------------------------------------

@pytest.fixture
def custom(host, tiny_model):
    """! @brief The tiny model as the picked custom weights; settings restored after."""
    dest = os.path.join(um.custom_dir(), "cim_test_tiny_x2.pth")
    with open(tiny_model, "rb") as s, open(dest, "wb") as d:
        d.write(s.read())
    old = host.config.get("upscale_esrgan_weights")
    host.set_config("upscale_esrgan_weights", dest, save=False)
    yield dest
    host.set_config("upscale_esrgan_weights", old or "", save=False)
    model_registry.clear("esrgan:")
    if os.path.exists(dest):
        os.remove(dest)


@pytest.fixture
def runner(app, host, monkeypatch):
    """! @brief The module's Jobs with the ingest wait done inline."""
    jobs = host.get_service("upscale")["run_job"].__self__

    def wait(job, qid, timeout=0):
        row = host.db().execute("SELECT * FROM upload_queue WHERE id=?", (qid,)).fetchone()
        outcome, detail, rel = app._process_upload_job(dict(row))
        db = host.db()
        db.execute("DELETE FROM upload_queue WHERE id=?", (qid,))
        db.commit()
        assert outcome == "done", detail
        return rel
    monkeypatch.setattr(jobs, "wait_ingest", wait)
    return jobs


def _job(jobs, rel, factor=0.0, mode="copy"):
    j = {"id": "t", "rel": rel, "factor": factor, "mode": mode, "user": "", "status": "running",
         "progress": 0, "message": "", "error": "", "result": "", "cancel": False}
    return jobs.run(j)


def _dims(host, rel):
    r = host.db().execute("SELECT width, height FROM files WHERE rel_path=?", (rel,)).fetchone()
    return (r["width"], r["height"])


def test_info_route(client, custom):
    j = client.get("/api/upscale/info").get_json()
    assert j["success"] and j["model"] == "cim_test_tiny_x2.pth" and j["available"]


def test_copy_keeps_metadata(client, host, custom, runner, upload):
    rel = upload("ups_src.png", seed=31, metadata=json.dumps({"tags": ["harbour"], "description": "boats"}))
    w, h = _dims(host, rel)
    new = _job(runner, rel)
    upload.made.append(new)
    assert new != rel and os.path.basename(new).startswith("ups_src_x2.")
    assert _dims(host, new) == (2 * w, 2 * h)
    meta = host.core.read_metadata(media_path(new))
    assert "harbour" in meta["tags"] and meta["description"] == "boats"
    assert host.core.file_data(new, "upscale")["source"] == rel
    # a second run picks a free name; x3 runs the x2 model twice and resizes
    new2 = _job(runner, rel, factor=3)
    upload.made.append(new2)
    assert os.path.basename(new2).startswith("ups_src_x3.") and _dims(host, new2) == (3 * w, 3 * h)


def test_replace_in_place(client, host, custom, runner, upload):
    rel = upload("ups_rep.png", seed=32, metadata=json.dumps({"tags": ["kept"]}))
    w, h = _dims(host, rel)
    assert _job(runner, rel, mode="replace") == rel
    assert _dims(host, rel) == (2 * w, 2 * h)
    assert "kept" in host.core.read_metadata(media_path(rel))["tags"]
    assert os.path.exists(media_path(os.path.join(".cim", "upscale_originals", rel)))


def test_route_queues_and_refuses(client, host, custom, upload):
    rel = upload("ups_route.png", seed=33)
    d = client.post("/api/upscale", json={"filenames": [rel, "nope/missing.png"], "factor": "2"}).get_json()
    assert d["success"] and len(d["jobs"]) == 1 and d["refused"][0]["error"] == "file not found"
    jid = d["jobs"][0]["id"]
    client.post("/api/upscale/cancel", json={"id": jid})
    assert any(j["id"] == jid for j in client.get("/api/upscale/jobs").get_json()["jobs"])
    bad = client.post("/api/upscale", json={"filename": rel, "factor": "40"})
    assert bad.status_code == 400


def test_replace_needs_its_permission(app, client, custom, upload):
    rel = upload("ups_perm.png", seed=34)
    user = {"id": 99, "username": "ups_user", "is_admin": False,
            "features": {"ai.upscale": 2, "data.upload": 2, "ai.upscale.replace": 0}}
    with signed_in_as(app, user):
        d = client.post("/api/upscale", json={"filename": rel, "mode": "replace"}).get_json()
    assert not d["success"] and d["refused"][0]["error"] == "you may not replace originals"


def test_weights_upload_and_delete(client, host, tiny_model):
    with open(tiny_model, "rb") as fh:
        data = fh.read()
    r = client.post("/api/upscale/weights/upload",
                    data={"file": (io.BytesIO(data), "cim_test_up.pth")}, content_type="multipart/form-data")
    j = r.get_json()
    assert j["success"] and j["scale"] == 2, j
    names = [c["name"] for c in client.get("/api/upscale/weights").get_json()["custom"]]
    assert "cim_test_up.pth" in names
    r = client.post("/api/upscale/weights/upload",
                    data={"file": (io.BytesIO(b"not a model"), "junk.pth")}, content_type="multipart/form-data")
    assert r.status_code == 400 and not os.path.exists(os.path.join(um.custom_dir(), "junk.pth"))
    assert client.post("/api/upscale/weights/delete", json={"name": "cim_test_up.pth"}).get_json()["success"]
    assert not os.path.exists(os.path.join(um.custom_dir(), "cim_test_up.pth"))