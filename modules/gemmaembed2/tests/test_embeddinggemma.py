"""! @file
@brief EmbeddingGemma 2 module: registration, settings validation and the
modality gating that greys a pick out when its encoder is not loaded. The
provider contracts themselves run in tests/test_providers.py."""
import inspect

import numpy as np

from modules.gemmaembed2 import module as eg


def test_no_remote_code_anywhere():
    """! @brief The point of the module: nothing executes code from the model repo."""
    assert "trust_remote_code=True" not in inspect.getsource(eg)


def test_modality_gating():
    assert eg._has("all", "image") and eg._has("all", "audio") and eg._has("all", "text")
    assert eg._has("text", "text") and not eg._has("text", "image") and not eg._has("text", "audio")
    assert eg._has("image", "image") and not eg._has("image", "audio")
    assert eg._has("audio", "audio") and not eg._has("audio", "image")


def test_validators():
    assert eg._clamp_dims(512) == 512 and eg._clamp_dims("256") == 256
    assert eg._clamp_dims(300) == 0 and eg._clamp_dims("") == 0 and eg._clamp_dims("x") == 0
    assert eg._clamp_modalities("text") == "text" and eg._clamp_modalities("video") == "all"


def test_normalise_truncates_and_renormalises():
    v = eg._normalise(np.ones(768, np.float32), 256)
    assert v.shape == (256,) and abs(float(np.linalg.norm(v)) - 1.0) < 1e-5
    assert eg._normalise(np.zeros(8, np.float32)) is None


def test_registered_in_all_three_capabilities(host):
    for cap in ("embed", "embed.text", "embed.audio"):
        assert "embeddinggemma" in host.broker._providers.get(cap, {}), cap
        