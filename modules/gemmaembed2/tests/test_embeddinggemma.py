"""! @file
@brief EmbeddingGemma 2 module: registration, settings validation and the
modality gating that greys a pick out when its encoder is not loaded. The
provider contracts themselves run in tests/test_providers.py."""
import inspect
import shutil
import subprocess

import numpy as np
import pytest

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
        

class _FakeST:
    """! @brief Stands in for SentenceTransformer.encode: records what goes in."""

    def __init__(self):
        self.calls = []

    def encode(self, items, **kw):
        self.calls.append((items, kw))
        return np.ones((len(items), 768), np.float32)


def _gemma(modalities="all"):
    g = object.__new__(eg._Gemma)
    g.modalities, g.model = modalities, _FakeST()
    return g


def test_versions_the_model_needs_are_declared():
    pip = " ".join(eg.MANIFEST["pip"])
    assert "transformers>=5.19" in pip and "sentence-transformers>=6.1" in pip


def test_image_goes_in_as_rgb_pil_without_prompt():
    g = _gemma()
    for img in (np.zeros((8, 6, 3), np.uint8), np.zeros((8, 6, 4), np.uint8), np.zeros((8, 6), np.uint8)):
        v = g.embed_image(img, 256)
        assert v.shape == (256,)
        item, kw = g.model.calls[-1]
        assert isinstance(item[0], eg.Image.Image) and item[0].mode == "RGB" and item[0].size == (6, 8)
        assert "prompt_name" not in kw and kw["truncate_dim"] == 256
    assert _gemma("text").embed_image(np.zeros((4, 4, 3), np.uint8), 0) is None


def test_text_prompts():
    g = _gemma("text")
    g.embed_text("a cat", 0)
    g.embed_text("a cat", 0, query=True)
    assert [c[1]["prompt_name"] for c in g.model.calls] == ["Document", "SearchQuery"]
    assert g.embed_text("  ", 0) is None


def test_audio_decoded_to_mono_16k(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    wav = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ac", "2", "-ar", "44100", str(wav)], check=True)
    wave = eg.decode_audio(str(wav))
    assert wave.dtype == np.float32 and abs(len(wave) - 16000) < 200
    g = _gemma()
    assert g.embed_audio(str(wav), 0) is not None
    item, kw = g.model.calls[-1]
    assert item[0]["sampling_rate"] == 16000 and len(item[0]["array"]) == len(wave)
    assert _gemma("image").embed_audio(str(wav), 0) is None
