"""
Personal aesthetic scorer: typed feature tokens -> Transformer encoder -> [CLS] -> score.

Input per image is a variable-length set of tokens, each of a declared type
(global embed, N image tiles, one per face, one per person pose, base-IQA,
hashed tags). Every type has its own projection to D; the sequence is padded
and masked, so adding a feature type is one more entry in `dims`.

Growth (Net2Net) is optional: deeper is exact (new blocks start as identity),
wider is a warm start (old weights copied into the top-left slice).
"""
import torch
import torch.nn as nn

TAG_BUCKETS = 4096   # ponytail: tags hashed into fixed buckets, no vocab file to keep in sync
TIERS = [(5000, 128, 2), (50000, 384, 6), (float("inf"), 768, 12)]   # (max_ratings, D, depth)


def tier_for(n_ratings):
    for cap, d, depth in TIERS:
        if n_ratings < cap:
            return d, depth
    return TIERS[-1][1:]


class Scorer(nn.Module):
    def __init__(self, dims, d=128, depth=2):
        """dims: {token_type: input_dim}."""
        super().__init__()
        self.dims, self.d, self.depth = dict(dims), d, depth
        self.proj = nn.ModuleDict({k: nn.Linear(v, d) for k, v in sorted(dims.items())})
        self.type_emb = nn.ParameterDict({k: nn.Parameter(torch.zeros(d)) for k in sorted(dims)})
        self.tag_emb = nn.Embedding(TAG_BUCKETS + 1, d, padding_idx=0)
        self.cls = nn.Parameter(torch.zeros(1, 1, d))
        self.blocks = nn.ModuleList([self._block(d) for _ in range(depth)])
        self.norm = nn.LayerNorm(d)
        self.head = nn.Linear(d, 1)

    @staticmethod
    def _block(d):
        return nn.TransformerEncoderLayer(d, nhead=8, dim_feedforward=4 * d,
                                          dropout=0.1, batch_first=True, norm_first=True)

    def forward(self, feats, masks, tags):
        """feats: {type: [B, N, dim]}, masks: {type: [B, N] bool valid}, tags: [B, T] long (0=pad).
        -> [B] logits."""
        B = tags.shape[0]
        toks = [self.cls.expand(B, -1, -1)]
        valid = [torch.ones(B, 1, dtype=torch.bool, device=tags.device)]
        for k in self.proj:
            if k in feats and feats[k].shape[1]:
                toks.append(self.proj[k](feats[k]) + self.type_emb[k])
                valid.append(masks[k])
        toks.append(self.tag_emb(tags)); valid.append(tags != 0)
        x, pad = torch.cat(toks, 1), ~torch.cat(valid, 1)
        for blk in self.blocks:
            x = blk(x, src_key_padding_mask=pad)
        return self.head(self.norm(x[:, 0])).squeeze(-1)

    # ── Net2Net ──────────────────────────────────────────────────────────
    def grow(self, d, depth):
        """New Scorer(d, depth) initialised from self (d, depth >= current)."""
        new = Scorer(self.dims, d, depth)
        with torch.no_grad():
            _copy_slice(new, self)                              # Net2WiderNet (warm start)
            for i in range(self.depth, depth):                 # Net2DeeperNet (exact identity)
                blk = new.blocks[i]
                blk.self_attn.out_proj.weight.zero_(); blk.self_attn.out_proj.bias.zero_()
                blk.linear2.weight.zero_(); blk.linear2.bias.zero_()
        return new


def _copy_slice(dst, src):
    """Copy every src tensor into the top-left corner of the same-named dst tensor.
    ponytail: exact function preservation through LayerNorm isn't possible with a
    plain slice copy; new dims start small-random and train in."""
    sd = src.state_dict()
    for k, t in dst.state_dict().items():
        if k not in sd:
            continue
        s = sd[k]
        if s.shape == t.shape:
            t.copy_(s); continue
        t.mul_(0.01)
        if "in_proj_" in k:                       # packed [q;k;v]: widen each chunk separately
            for dc, sc in zip(t.chunk(3, 0), s.chunk(3, 0)):
                dc[tuple(slice(0, n) for n in sc.shape)] = sc
            continue
        t[tuple(slice(0, n) for n in s.shape)] = s


def hash_tags(tag_names, max_len=32):
    """Tag names -> stable bucket ids (1..TAG_BUCKETS), padded with 0."""
    import zlib
    ids = [1 + zlib.crc32(t.lower().encode()) % TAG_BUCKETS for t in tag_names][:max_len]
    return ids + [0] * (max_len - len(ids))


def batch(samples, dims, device):
    """samples: [{type: [[floats], ...], "tags": [ids]}] -> (feats, masks, tags) padded tensors."""
    feats, masks = {}, {}
    for k, n in dims.items():
        rows = [s.get(k) or [] for s in samples]
        N = max(len(r) for r in rows)
        f = torch.zeros(len(rows), N, n); m = torch.zeros(len(rows), N, dtype=torch.bool)
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                v = list(v)[:n]; f[i, j, :len(v)] = torch.tensor(v); m[i, j] = True
        feats[k], masks[k] = f.to(device), m.to(device)
    tags = torch.tensor([s.get("tags") or [0] for s in samples], dtype=torch.long, device=device)
    return feats, masks, tags


def spearman(a, b):
    import numpy as np
    def rank(x):
        r = np.empty(len(x)); r[np.argsort(x)] = np.arange(len(x)); return r
    if len(a) < 3:
        return 0.0
    ra, rb = rank(np.asarray(a, float)), rank(np.asarray(b, float))
    return float(np.corrcoef(ra, rb)[0, 1])


if __name__ == "__main__":   # self-check: padding + exact deeper growth + tier table
    dims = {"embed": 16, "tile": 16, "pose17": 34}
    m = Scorer(dims, 32, 1).eval()
    samples = [{"embed": [[0.1] * 16], "tile": [[0.2] * 16] * 4, "pose17": [], "tags": hash_tags(["a", "b"])},
               {"embed": [[0.3] * 16], "tile": [[0.4] * 16] * 2, "pose17": [[0.5] * 34], "tags": hash_tags([])}]
    f, mk, t = batch(samples, dims, "cpu")
    assert f["tile"].shape == (2, 4, 16) and mk["tile"].sum().item() == 6
    g = m.grow(32, 3).eval()
    assert torch.allclose(m(f, mk, t), g(f, mk, t), atol=1e-5), "deeper grow broke identity"
    assert g.grow(64, 4)(f, mk, t).shape == (2,)
    assert tier_for(100) == (128, 2) and tier_for(60000) == (768, 12)
    assert abs(spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9
    print("ok")