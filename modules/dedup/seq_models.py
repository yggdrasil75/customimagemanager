"""
Sequence duplicate model — the shared core of HEURDUV (video) and HEARDU (audio).
======================================================================
A timeline (video frames at media_sig.VIDEO_FPS, audio as 1 s log-mel
windows every 0.5 s) becomes one embedding per step:

    step encoder (2D CNN, per subclass)  ->  [T, C]
    temporal block: `depth` residual Conv1d(k=3) over T
    L2 normalise

Two timelines compare as a step x step matrix S = sigmoid(a * cos + b)
(a, b learned: the calibration of "same content"), and closed-end DTW
(seq_align) turns S into the shared fraction of the longer timeline — the
same rule the image, phash and naive scores use, so a trimmed copy, a
re-encode at another fps, a re-cut all land where they should.

Training (modules/dedup_train/build_seq) feeds (a_steps, b_steps, map):
map[j] = the step of a that b's step j shows, or -1 (unrelated / edited
over). Loss: per-cell BCE on S — S[map[j], j] -> 1, cells further than
`tol` steps from it -> 0, a -1 column -> all 0; positives up-weighted to
balance. Everything is a no-op without torch (available False).

Subclasses set FAMILY / ARCH, build `_step_encoder(C)` and convert a file
to steps in `steps_from_path`.
"""

import io
import os
import time

import numpy as np

from optional_deps import optional_import
torch, _HAVE_TORCH = optional_import("torch")
nn = torch.nn if _HAVE_TORCH else None

from . import seq_align

BASE_CH = 64
SIZES = {
    "nano":   {"width": 0.25, "depth": 1},
    "small":  {"width": 0.5,  "depth": 2},
    "medium": {"width": 1.0,  "depth": 2},
    "large":  {"width": 2.0,  "depth": 3},
}
EMBED_CHUNK = 64          # steps per encoder pass at inference


def ch(width: float) -> int:
    return max(16, int(round(BASE_CH * float(width))))


def parse_sizes(text) -> dict:
    """"name width depth" per line; empty / invalid -> SIZES."""
    out = {}
    for line in str(text or "").splitlines():
        p = [x for x in line.replace(",", " ").split() if x]
        if len(p) < 2 or p[0].startswith("#"):
            continue
        try:
            out[p[0].lower()] = {"width": min(8.0, max(0.125, float(p[1]))),
                                 "depth": max(1, int(p[2])) if len(p) > 2 else 1}
        except ValueError:
            continue
    return out or {k: dict(v) for k, v in SIZES.items()}


def sizes_text(sizes=None) -> str:
    return "\n".join(f"{k} {v['width']} {v['depth']}" for k, v in (sizes or SIZES).items())


if _HAVE_TORCH:
    def _gn(c):
        return nn.GroupNorm(min(8, c), c)       # no batch statistics: a timeline is embedded alone

    def conv_block(cin, cout, stride=2):
        return [nn.Conv2d(cin, cout, 3, stride, 1), _gn(cout), nn.ReLU(inplace=True)]

    class _SeqNet(nn.Module):
        def __init__(self, step_encoder, C: int, depth: int) -> None:
            super().__init__()
            self.step = step_encoder
            self.temporal = nn.ModuleList(
                nn.Sequential(nn.Conv1d(C, C, 3, padding=1), _gn(C), nn.ReLU(inplace=True))
                for _ in range(max(1, int(depth))))
            self.a = nn.Parameter(torch.tensor(10.0))
            self.b = nn.Parameter(torch.tensor(-7.0))

        def embed(self, x):
            """x [T, ...] -> [T, C] unit vectors (one timeline)."""
            e = self.step(x)                                 # [T, C]
            h = e.t()[None]                                  # [1, C, T]
            for blk in self.temporal:
                h = h + blk(h)
            return nn.functional.normalize(h[0].t(), dim=1)

        def logits(self, ea, eb):
            return self.a * (ea @ eb.t()) + self.b


class SeqDupModel:
    """! @brief Base: sizes, load/save, embed, score, fit. Subclasses: HEURDUV, HEARDU."""

    FAMILY = "seq"
    ARCH = "seqdup"
    STEP_SHAPE = (3, 16, 16)      # one step's tensor shape (subclass)
    SIZES = SIZES

    def __init__(self, width: float = 1.0, depth: int = 2, size: str = "") -> None:
        self.width, self.depth, self.size = float(width), max(1, int(depth)), size
        self.trained, self.error = False, ""
        self.net = _SeqNet(self._step_encoder(ch(self.width)), ch(self.width), self.depth) if _HAVE_TORCH else None

    # ── subclass hooks ───────────────────────────────────────────────────
    def _step_encoder(self, C: int):
        raise NotImplementedError

    @staticmethod
    def prep(x: np.ndarray) -> np.ndarray:
        """Stored steps (e.g. uint8 frames) -> float32 [T, *STEP_SHAPE]. Identity by default."""
        return np.asarray(x, np.float32)

    @staticmethod
    def steps_from_path(path: str) -> "np.ndarray | None":
        """File -> float32 steps [T, *STEP_SHAPE] (subclass)."""
        raise NotImplementedError

    # ── plumbing ─────────────────────────────────────────────────────────
    @classmethod
    def sized(cls, size: str, sizes: "dict | None" = None):
        tbl = sizes or cls.SIZES
        sp = tbl.get(str(size).lower()) or tbl.get("medium") or next(iter(tbl.values()))
        return cls(sp["width"], sp["depth"], size=str(size).lower())

    @property
    def available(self) -> bool:
        return _HAVE_TORCH and self.net is not None

    @property
    def params(self) -> int:
        return sum(p.numel() for p in self.net.parameters()) if self.available else 0

    @classmethod
    def load(cls, path: str):
        m = cls()
        if not _HAVE_TORCH:
            return m
        try:
            ck = torch.load(path, map_location="cpu")
            if ck.get("arch") != cls.ARCH:
                raise ValueError(f"not a {cls.FAMILY} checkpoint (arch {ck.get('arch')!r})")
            m = cls(float(ck["width"]), int(ck["depth"]), size=str(ck.get("size", "")))
            m.net.load_state_dict(ck["state_dict"])
            m.net.eval()
            m.trained = True
        except Exception as e:
            m.trained, m.error = False, f"{type(e).__name__}: {e}"
        return m

    def save(self, path: str) -> bool:
        if not self.available:
            return False
        try:
            tmp = path + ".tmp"
            torch.save({"arch": self.ARCH, "family": self.FAMILY, "state_dict": self.net.state_dict(),
                        "width": self.width, "depth": self.depth, "size": self.size, "saved": time.time()}, tmp)
            os.replace(tmp, path)
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            return False

    # ── inference ────────────────────────────────────────────────────────
    def embed(self, steps: np.ndarray, device: str = "cpu"):
        """steps [T, ...] float32 -> torch [T, C] (no grad)."""
        self.net.to(device).eval()
        with torch.no_grad():
            x = torch.from_numpy(np.ascontiguousarray(self.prep(steps), np.float32))
            e = torch.cat([self.net.step(x[s:s + EMBED_CHUNK].to(device)) for s in range(0, len(x), EMBED_CHUNK)])
            h = e.t()[None]
            for blk in self.net.temporal:
                h = h + blk(h)
            return nn.functional.normalize(h[0].t(), dim=1)

    def sim_matrix(self, ea, eb) -> np.ndarray:
        with torch.no_grad():
            return torch.sigmoid(self.net.logits(ea, eb)).float().cpu().numpy()

    def score_steps(self, steps_a, steps_b, device: str = "cpu") -> "float | None":
        if not (self.available and self.trained) or steps_a is None or steps_b is None \
                or not len(steps_a) or not len(steps_b):
            return None
        S = self.sim_matrix(self.embed(steps_a, device), self.embed(steps_b, device))
        return seq_align.dtw_score(1.0 - S)

    def score_paths(self, path_a: str, path_b: str, device: str = "cpu") -> "float | None":
        return self.score_steps(self.steps_from_path(path_a), self.steps_from_path(path_b), device)

    # ── training ─────────────────────────────────────────────────────────
    def fit_batches(self, batches, lr: float = 1e-3, device: str = "cpu", _opt_holder: dict = None,
                    tol: int = 2) -> "float | None":
        """!
        @brief One pass over batches of pairs [(a [Ta,...], b [Tb,...], map [Tb] int), ...].
        @return mean loss, or None without torch.
        """
        if not self.available:
            return None
        self.net.to(device).train()
        holder = _opt_holder if _opt_holder is not None else {}
        opt = holder.get("opt")
        if opt is None or holder.get("lr") != lr:
            opt = torch.optim.Adam(self.net.parameters(), lr=lr)
            holder["opt"], holder["lr"] = opt, lr
            if holder.get("opt_state"):
                try:
                    opt.load_state_dict(holder.pop("opt_state"))
                except Exception:
                    holder.pop("opt_state", None)
        total, n = 0.0, 0
        for batch in batches:
            if not batch:
                continue
            opt.zero_grad(set_to_none=True)
            loss = 0.0
            for a, b, mp in batch:
                ta = torch.from_numpy(np.ascontiguousarray(self.prep(a), np.float32)).to(device)
                tb = torch.from_numpy(np.ascontiguousarray(self.prep(b), np.float32)).to(device)
                L = self.net.logits(self.net.embed(ta), self.net.embed(tb))      # [Ta, Tb]
                tgt, w = pair_targets(len(a), np.asarray(mp), tol)
                tgt_t = torch.from_numpy(tgt).to(device)
                w_t = torch.from_numpy(w).to(device)
                loss = loss + (nn.functional.binary_cross_entropy_with_logits(L, tgt_t, reduction="none")
                               * w_t).sum() / max(1.0, float(w.sum()))
            loss = loss / len(batch)
            loss.backward()
            opt.step()
            total += float(loss.item()) * len(batch); n += len(batch)
        self.net.eval()
        self.trained = self.trained or n > 0
        return total / n if n else None

    def bench(self, device: str = "cpu", steps: int = 120) -> dict:
        if not self.available:
            return {}
        x = np.random.rand(steps, *self.STEP_SHAPE).astype(np.float32)
        self.embed(x[:4], device)
        t = time.perf_counter()
        self.embed(x, device)
        return {"params": self.params, "device": device,
                "ms_per_step": round((time.perf_counter() - t) / steps * 1000, 3)}


def pair_targets(ta: int, mp: np.ndarray, tol: int = 2) -> "tuple[np.ndarray, np.ndarray]":
    """(target [Ta, Tb] 0/1, weight [Ta, Tb]) for a step map (map[j] in 0..Ta-1 or -1).
    Cells within tol of a match but not on it are ignored (weight 0): adjacent
    steps of slow footage legitimately look the same. Positives are weighted
    up to balance the negatives."""
    tb = len(mp)
    tgt = np.zeros((ta, tb), np.float32)
    w = np.ones((ta, tb), np.float32)
    rows = np.arange(ta)[:, None]
    for j, i in enumerate(mp.tolist()):
        if i < 0:
            continue
        near = np.abs(rows[:, 0] - i) <= tol
        w[near, j] = 0.0
        tgt[i, j] = 1.0
        w[i, j] = 1.0
    pos = tgt.sum()
    if pos:
        w[tgt > 0] *= min(50.0, max(1.0, float((w * (1 - tgt)).sum()) / pos))
    return tgt, w


def subsample_pair(a, b, mp, cap: int = 64):
    """Keep <= cap evenly spaced steps of each side, remapping map (a step of b
    whose partner was dropped maps to the nearest kept step of a, if within 1)."""
    ia, ib = seq_align.resample_idx(len(a), cap), seq_align.resample_idx(len(b), cap)
    mp = np.asarray(mp)
    out = np.full(len(ib), -1, np.int64)
    for k, j in enumerate(ib.tolist()):
        i = int(mp[j]) if j < len(mp) else -1
        if i >= 0:
            q = int(np.argmin(np.abs(ia - i)))
            if abs(int(ia[q]) - i) <= max(1, len(a) // max(1, cap)):
                out[k] = q
    return a[ia], b[ib], out


def pack_steps(a: np.ndarray, b: np.ndarray, mp, cap: int = 64) -> bytes:
    """Feedback sample: two step arrays (stored in their own dtype; uint8 frames
    stay uint8) + the step map, at most `cap` steps a side."""
    a, b, mp = subsample_pair(np.asarray(a), np.asarray(b), mp, cap)
    if a.dtype != np.uint8:
        a, b = a.astype(np.float16), b.astype(np.float16)
    buf = io.BytesIO()
    np.savez_compressed(buf, a=a, b=b, map=np.asarray(mp, np.int32))
    return buf.getvalue()


def unpack_steps(blob: bytes):
    """-> (a, b, map) as stored (uint8 or float32)."""
    d = np.load(io.BytesIO(blob))
    a, b = d["a"], d["b"]
    if a.dtype != np.uint8:
        a, b = a.astype(np.float32), b.astype(np.float32)
    return a, b, d["map"].astype(np.int64)
