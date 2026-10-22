"""! @file
@brief MuQ on current transformers: a small random MuQ (no download) builds and embeds
with the module's compat shims - its EasyDict config gets _attn_implementation, and
the Conformer encoder returns hidden_states again (transformers 5 dropped them)."""
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("muq")
pytest.importorskip("easydict")
from muq import MuQ                                                        # noqa: E402
from muq.muq import MuQConfig                                              # noqa: E402
from transformers import Wav2Vec2ConformerConfig                           # noqa: E402

from modules.muq_embed.module import _easydict_attn_compat, _hidden_states_compat  # noqa: E402


def _tiny_muq():
    base = {k: v for k, v in Wav2Vec2ConformerConfig().to_dict().items()
            if not k.startswith("_") and k not in ("id2label", "label2id", "transformers_version", "architectures")}
    cfg = MuQConfig(label_rate=25, num_codebooks=1, codebook_dim=16, codebook_size=4096,
                    features=["melspec_2048"], hop_length=240, n_mels=128, conv_dim=128, encoder_dim=256,
                    encoder_depth=2, mask_hop=0.4, mask_prob=0.6,
                    stat={"melspec_2048_mean": 6.7, "melspec_2048_std": 18.4},
                    w2v2_config={**base, "num_attention_heads": 4, "intermediate_size": 512,
                                 "position_embeddings_type": "rotary", "conv_depthwise_kernel_size": 5})
    with _easydict_attn_compat():
        m = MuQ(cfg).eval()
    return _hidden_states_compat(m)


def test_muq_embeds_on_this_transformers():
    from easydict import EasyDict
    m = _tiny_muq()
    assert "_attn_implementation" not in EasyDict.__dict__              # the class is left as it was
    assert "_attn_implementation" not in EasyDict({"a": 1})
    x = torch.randn(1, 24000 * 2)
    with torch.no_grad():
        out = m(x, output_hidden_states=False)
        full = m(x, output_hidden_states=True)
    assert out.last_hidden_state.shape == (1, 50, 256)
    assert len(full.hidden_states) == 3                                  # input of each layer + final norm
    assert torch.allclose(full.hidden_states[-1], out.last_hidden_state)
    assert _hidden_states_compat(m) is m                                 # wrapping twice is a no-op