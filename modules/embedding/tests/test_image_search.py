"""! @file
@brief Image-to-image search with an uploaded picture (the search box's image
popover): /api/embedding/search_image takes a multipart `file` as well as a
library filename, and answers gallery entries best first."""
import io

import numpy as np
from cimtest import png_bytes, post_json


def _mean_colour(img, *a, **k):
    """! @brief Fake embedder: the picture's mean colour, so solid colours separate."""
    v = np.asarray(img, np.float32).reshape(-1, img.shape[-1] if img.ndim == 3 else 1).mean(0) + 1.0
    return v / np.linalg.norm(v)


def _store(upload, name, colour):
    """! @brief Upload a solid-colour PNG -> stored filename."""
    j = upload._post({"file": (io.BytesIO(png_bytes(color=colour)), name),
                      "mode": "sync", "folder": ""}, name)
    return j["filename"]


def test_search_image_upload_ranks_matching_colour_first(client, upload, fake_model):
    fake_model("embed", _mean_colour)
    red = _store(upload, "is_red.png", (0, 0, 250))
    green = _store(upload, "is_green.png", (0, 250, 0))
    j = post_json(client, "/api/embedding/generate", {"filenames": [red, green], "force": True})
    assert j["success"], j
    r = client.post("/api/embedding/search_image", content_type="multipart/form-data",
                    data={"file": (io.BytesIO(png_bytes(color=(0, 240, 10))), "query.png"),
                          "top_k": "5"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["success"], d
    ranked = [x["filename"] for x in d["results"] if x["filename"] in (red, green)]
    assert ranked[0] == green, ranked
    files = [f["filename"] for f in d["files"] if f["filename"] in (red, green)]
    assert files == ranked and all(f["kind"] == "image" for f in d["files"])


def test_search_image_by_filename_returns_entries(client, upload, fake_model):
    fake_model("embed", _mean_colour)
    red = _store(upload, "is_red2.png", (0, 0, 240))
    post_json(client, "/api/embedding/generate", {"filenames": [red], "force": True})
    d = post_json(client, "/api/embedding/search_image", {"filename": red, "top_k": 3})
    assert d["success"], d
    assert d["files"] and d["files"][0]["filename"] == red


def test_search_image_rejects_bad_upload_and_empty_body(client, fake_model):
    fake_model("embed", _mean_colour)
    r = client.post("/api/embedding/search_image", content_type="multipart/form-data",
                    data={"file": (io.BytesIO(b"not an image"), "x.png")})
    assert r.status_code == 400 and not r.get_json()["success"]
    d = post_json(client, "/api/embedding/search_image", {})
    assert not d["success"]
