"""! @file
@brief Personal aesthetic scorer: typed feature tokens -> Transformer encoder -> [CLS] -> score.

Input per image is a variable-length set of tokens, each of a declared type
(global embed, N image tiles, one per face, one per person pose, base-IQA,
hashed tags). Every type has its own projection to D; the sequence is padded
and masked, so adding a feature type is one more entry in `dims`.

Growth (Net2Net) is optional: deeper is exact (new blocks start as identity),
wider is a warm start (old weights copied into the top-left slice).

Mode: a "mode" token [detail 0..1, vigorous 0/1] is both a token and a gate.
Every token type's embeddings are scaled by sigmoid(gate(mode)), so the net
can learn "simple mode: ignore tiles/regions/small objects, score the
foreground; vigorous mode: attend to the background and the small stuff".
simplify() makes the simple-mode view of a vigorous sample for training.
"""
import torch
import torch.nn as nn
import zlib

import numpy as np

TAG_BUCKETS = 4096   # ponytail: tags hashed into fixed buckets, no vocab file to keep in sync
TIERS = [(500, "nano"), (2000, "small"), (5000, "medium"), (20000, "large"), (50000, "xl"),
         (float("inf"), "xxl")]   # (max_ratings, size name): the pretrained size to fine-tune at that count


def tier_for(n_ratings, sizes=None):
    """! @brief -> (size name, d, depth) for this many ratings. Unknown names in a custom
    size table fall back to the last size listed."""
    sizes = sizes or SIZES
    name = next(nm for cap, nm in TIERS if n_ratings < cap)
    sp = sizes.get(name) or list(sizes.values())[-1]
    return name, sp["d"], sp["depth"]


MODE_DIM = 2                 # [detail level 0..1, vigorous flag]
SIMPLE_CAPS = {"tile": 4, "object": 3, "region": 3}   # simple mode keeps this many of each (see simplify)


def simplify(feats):
    """! @brief Simple-mode view of a sample: flag 0, token lists cut to SIMPLE_CAPS
    (tiles are first-N of the grid, ponytail: not a re-tiling)."""
    out = dict(feats)
    for k, n in SIMPLE_CAPS.items():
        out[k] = (feats.get(k) or [])[:n]
        if k + "_raw" in feats:
            out[k + "_raw"] = (feats.get(k + "_raw") or [])[:n]
    out["mode"] = [[(feats.get("mode") or [[0.5, 1.0]])[0][0], 0.0]]
    return out


# Named size table for pretraining (iqa_train). Editable via the
# personal_iqa_sizes setting: one "name d depth" per line. d must be a
# multiple of 8 (8 attention heads).
SIZES = {"nano": {"d": 64, "depth": 1}, "small": {"d": 128, "depth": 2},
         "medium": {"d": 256, "depth": 4}, "large": {"d": 384, "depth": 6},
         "xl": {"d": 512, "depth": 8}, "xxl": {"d": 768, "depth": 12}}


def parse_sizes(text):
    """! @brief "name d depth" lines -> {name: {d, depth}}; empty/invalid -> SIZES."""
    out = {}
    for line in str(text or "").splitlines():
        p = [x for x in line.replace(",", " ").replace(":", " ").split() if x]
        if len(p) < 2 or p[0].startswith("#"):
            continue
        try:
            d = max(8, int(p[1]) // 8 * 8)
            depth = max(1, int(p[2])) if len(p) > 2 else 1
        except ValueError:
            continue
        out[p[0].lower()] = {"d": d, "depth": depth}
    return out or {k: dict(v) for k, v in SIZES.items()}


def sizes_text(sizes=None):
    return "\n".join(f"{k} {v['d']} {v['depth']}" for k, v in (sizes or SIZES).items())


def count_params(dims, d, depth):
    """! @brief Parameter count without building the net (tags + typed projections + blocks + head)."""
    n = sum((v + 1) * d + d for v in dims.values())            # proj + type_emb per token type
    n += (TAG_BUCKETS + 1) * d + d + (MODE_DIM + 1) * len(dims)  # tag_emb + cls + mode gate
    per_block = 4 * d * d + 4 * d + 2 * (4 * d * d) + 4 * d + d + 4 * d   # attn + ffn + 2 layernorms
    return n + depth * per_block + 2 * d + d + 1


class Scorer(nn.Module):
    def __init__(self, dims, d=128, depth=2):
        """! @brief dims: {token_type: input_dim}."""
        super().__init__()
        self.dims, self.d, self.depth = dict(dims), d, depth
        self.proj = nn.ModuleDict({k: nn.Linear(v, d) for k, v in sorted(dims.items())})
        self.type_emb = nn.ParameterDict({k: nn.Parameter(torch.zeros(d)) for k in sorted(dims)})
        self.tag_emb = nn.Embedding(TAG_BUCKETS + 1, d, padding_idx=0)
        self.cls = nn.Parameter(torch.zeros(1, 1, d))
        self.gate = nn.Linear(MODE_DIM, len(self.proj))          # per-type scale from the mode token
        nn.init.zeros_(self.gate.weight); nn.init.constant_(self.gate.bias, 3.0)   # starts ~open (0.95)
        self.blocks = nn.ModuleList([self._block(d) for _ in range(depth)])
        self.norm = nn.LayerNorm(d)
        self.head = nn.Linear(d, 1)

    @staticmethod
    def _block(d):
        return nn.TransformerEncoderLayer(d, nhead=8, dim_feedforward=4 * d,
                                          dropout=0.1, batch_first=True, norm_first=True)

    def forward(self, feats, masks, tags):
        """! @brief feats: {type: [B, N, dim]}, masks: {type: [B, N] bool valid}, tags: [B, T] long (0=pad).
        -> [B] logits."""
        B = tags.shape[0]
        toks = [self.cls.expand(B, -1, -1)]
        valid = [torch.ones(B, 1, dtype=torch.bool, device=tags.device)]
        mode = feats.get("mode")
        if mode is not None and mode.shape[1]:
            g = torch.sigmoid(self.gate(mode[:, 0, :MODE_DIM]))                 # [B, types]
        else:
            g = torch.ones(B, len(self.proj), device=tags.device)
        for i, k in enumerate(self.proj):
            if k in feats and feats[k].shape[1]:
                toks.append((self.proj[k](feats[k]) + self.type_emb[k]) * g[:, i, None, None])
                valid.append(masks[k])
        toks.append(self.tag_emb(tags)); valid.append(tags != 0)
        x, pad = torch.cat(toks, 1), ~torch.cat(valid, 1)
        for blk in self.blocks:
            x = blk(x, src_key_padding_mask=pad)
        return self.head(self.norm(x[:, 0])).squeeze(-1)

    # -- Net2Net ----------------------------------------------------------
    def grow(self, d, depth, dims=None):
        """! @brief New Scorer(d, depth) initialised from self (d, depth >= current).
        dims: a superset of self.dims; token types new to the checkpoint start fresh."""
        new = Scorer({**self.dims, **(dims or {})}, d, depth)
        with torch.no_grad():
            _copy_slice(new, self)                              # Net2WiderNet (warm start)
            for i in range(self.depth, depth):                 # Net2DeeperNet (exact identity)
                blk = new.blocks[i]
                blk.self_attn.out_proj.weight.zero_(); blk.self_attn.out_proj.bias.zero_()
                blk.linear2.weight.zero_(); blk.linear2.bias.zero_()
        return new


def _copy_slice(dst, src):
    """! @brief Copy every src tensor into the top-left corner of the same-named dst tensor.
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


VAR_DIMS = {"embed": "embed", "tile": "embed", "object": "embed", "region": "embed", "tag_text": "tag_text",
            "box_tag": "box_tag"}


def infer_dims(samples, fixed):
    """! @brief {token_type: dim} for Scorer: fixed dims plus the encoder-sized types
    (embed/tile/object/region share the image encoder's width, tag_text the
    text encoder's), read from the first sample that has each; 1 if none."""
    found = {}
    for s in samples:
        fe = s.get("feats", s)
        for k, src in VAR_DIMS.items():
            if src not in found and fe.get(k):
                found[src] = len(fe[k][0])
        if len(found) == len(set(VAR_DIMS.values())):
            break
    return {**{k: found.get(src, 1) for k, src in VAR_DIMS.items()}, **fixed}


def hash_tags(tag_names, max_len=None):
    """! @brief Tag names -> stable bucket ids (1..TAG_BUCKETS). All of them: the scorer has
    no positional encoding, so order never matters, and batch() pads per batch.
    max_len only pads/cuts to a fixed width when a caller needs one."""
    ids = [1 + zlib.crc32(t.lower().encode()) % TAG_BUCKETS for t in tag_names]
    return ids if max_len is None else (ids + [0] * max_len)[:max_len]


def batch(samples, dims, device):
    """! @brief samples: [{type: [[floats], ...], "tags": [ids]}] -> (feats, masks, tags) padded tensors."""
    feats, masks = {}, {}
    for k, n in dims.items():
        rows = [s.get(k) or [] for s in samples]
        N = max(len(r) for r in rows)
        f = torch.zeros(len(rows), N, n); m = torch.zeros(len(rows), N, dtype=torch.bool)
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                v = list(v)[:n]; f[i, j, :len(v)] = torch.tensor(v); m[i, j] = True
        feats[k], masks[k] = f.to(device), m.to(device)
    rows = [[int(x) for x in (s.get("tags") or []) if x] or [0] for s in samples]
    T = max(len(r) for r in rows)
    tags = torch.tensor([r + [0] * (T - len(r)) for r in rows], dtype=torch.long, device=device)
    return feats, masks, tags


def spearman(a, b):
    def rank(x):
        r = np.empty(len(x)); r[np.argsort(x)] = np.arange(len(x)); return r
    if len(a) < 3:
        return 0.0
    ra, rb = rank(np.asarray(a, float)), rank(np.asarray(b, float))
    return float(np.corrcoef(ra, rb)[0, 1])


if __name__ == "__main__":   # self-check: padding + exact deeper growth + tier table
    dims = {"embed": 16, "tile": 16, "pose17": 34}
    m = Scorer(dims, 32, 1).eval()
    samples = [{"embed": [[0.1] * 16], "tile": [[0.2] * 16] * 4, "pose17": [], "tags": hash_tags(["a", "b", "c"])},
               {"embed": [[0.3] * 16], "tile": [[0.4] * 16] * 2, "pose17": [[0.5] * 34], "tags": hash_tags([])}]
    f, mk, t = batch(samples, dims, "cpu")
    assert f["tile"].shape == (2, 4, 16) and mk["tile"].sum().item() == 6
    assert t.shape == (2, 3) and t[1].tolist() == [0, 0, 0] and len(hash_tags(["x"] * 40)) == 40
    g = m.grow(32, 3).eval()
    assert torch.allclose(m(f, mk, t), g(f, mk, t), atol=1e-5), "deeper grow broke identity"
    g2 = g.grow(32, 3, dims={"style": 20}).eval()           # new token type, absent in samples: unchanged
    assert torch.allclose(m(f, mk, t), g2(*batch(samples, g2.dims, "cpu")), atol=1e-5)
    assert infer_dims([{"feats": s} for s in samples], {"iqa": 1}) == \
        {"embed": 16, "tile": 16, "object": 16, "region": 16, "tag_text": 1, "box_tag": 1, "iqa": 1}
    assert g.grow(64, 4)(f, mk, t).shape == (2,)
    assert tier_for(100) == ("nano", 64, 1) and tier_for(60000) == ("xxl", 768, 12)
    assert tier_for(3000, {"only": {"d": 8, "depth": 1}}) == ("medium", 8, 1)
    md = {**dims, "mode": MODE_DIM}
    mm = Scorer(md, 32, 1).eval()
    with torch.no_grad():
        mm.gate.weight[:, 1] = -20                           # vigorous=0 closes every gate
    vig = [{**s, "mode": [[0.9, 1.0]]} for s in samples]
    assert not torch.allclose(mm(*batch(vig, md, "cpu")), mm(*batch([simplify(s) for s in vig], md, "cpu")))
    assert len(simplify(vig[0])["tile"]) == 4 and simplify(vig[0])["mode"] == [[0.9, 0.0]]
    assert sum(p.numel() for p in mm.parameters()) == count_params(md, 32, 1)
    assert abs(spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9
    print("ok")