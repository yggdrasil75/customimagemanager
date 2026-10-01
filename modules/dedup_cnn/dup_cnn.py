"""!
@brief Native-resolution change network for duplicate detection (HEURDU).

Nothing is downscaled. Every image goes through a stride-8 stem that is a
pure rearrangement (PixelUnshuffle: each 8x8x3 block becomes one 192-channel
cell), then a few dilated 3x3 blocks at that resolution, producing a per-cell
feature grid. A light head compares two grids (|fa-fb|, fa*fb) into a
per-cell CHANGE MAP: one probability per 8x8 block of the reference that the
other image differs there. The pair score is the unchanged fraction of the
map over the aligned overlap.

Encode once, compare many: a group of N images costs N encoder passes and
N(N-1)/2 cheap head passes on feature grids, giving an NxN matrix.

Images are aligned before comparison (ORB + RANSAC similarity transform, the
other image warped onto the reference), so crops, re-frames and scaled
copies compare where they overlap; framing that cannot be aligned is a
different picture. Very large images are encoded in overlapping strips —
a memory bound only, never a resolution cut.

Trained on native-resolution crops with a per-pixel change mask target
(modules/dedup_train). Sizes scale width (cells' channels) and depth (body
blocks); checkpoints carry their own width/depth.
"""

import io
import numpy as np
import os
import time

from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

try:
    import torch
    from torch import nn
    _HAVE_TORCH = True
except Exception:
    _HAVE_TORCH = False

STRIDE: int = 8            # pixels per cell (PixelUnshuffle factor); lossless
WORK: int = 256            # side of a stored feedback sample / training crop
WIDTH_MIN: float = 0.125
WIDTH_MAX: float = 8.0
BASE_CH: int = 64

SIZES: "dict[str, dict]" = {
    "nano":   {"width": 0.25, "depth": 1},
    "small":  {"width": 0.5,  "depth": 2},
    "medium": {"width": 1.0,  "depth": 3},
    "large":  {"width": 2.0,  "depth": 4},
    "xl":     {"width": 3.0,  "depth": 6},
    "xxl":    {"width": 4.0,  "depth": 8},
}
SIZE_ORDER = list(SIZES)


def parse_sizes(text: str) -> "dict[str, dict]":
    """! @brief User size table: one "name width depth" per line. Empty/invalid -> defaults."""
    out = {}
    for line in str(text or "").splitlines():
        parts = [x for x in line.replace(",", " ").replace(":", " ").split() if x]
        if len(parts) < 2 or parts[0].startswith("#"):
            continue
        try:
            w = min(WIDTH_MAX, max(WIDTH_MIN, float(parts[1])))
            d = max(1, int(parts[2])) if len(parts) > 2 else 1
        except ValueError:
            continue
        out[parts[0].lower()] = {"width": w, "depth": d}
    return out or {k: dict(v) for k, v in SIZES.items()}


def sizes_text(sizes: "dict[str, dict] | None" = None) -> str:
    return "\n".join(f"{k} {v['width']} {v['depth']}" for k, v in (sizes or SIZES).items())


def size_spec(size: str, sizes: "dict | None" = None) -> "dict":
    tbl = sizes or SIZES
    return dict(tbl.get(str(size).lower()) or tbl.get("medium") or next(iter(tbl.values())))


def _ch(width_mult: float) -> int:
    return max(8, int(round(BASE_CH * width_mult)))


def _dilations(depth: int) -> "list[int]":
    return [(1, 2, 4)[i % 3] for i in range(max(1, int(depth)))]


def count_params(width_mult: float, depth: int = 1) -> int:
    """! @brief Parameter count of a size without building it (no torch needed)."""
    C, d = _ch(width_mult), max(1, int(depth))
    stem = 3 * STRIDE * STRIDE * C + C + 2 * C
    body = d * (9 * C * C + C + 2 * C)
    head = 2 * C * C * 9 + C + 2 * C + C + 1
    return stem + body + head


def margin_px(depth: int) -> int:
    """! @brief Pixels of context a cell's feature depends on beyond itself (strip overlap)."""
    return STRIDE * (2 * sum(_dilations(depth)) + 2)


def _bgr3(img):
    if img is None:
        return None
    if img.ndim == 2:
        img = np.repeat(img[:, :, None], 3, axis=2)
    return np.ascontiguousarray(img[:, :, :3])


def _to_work_u8(img: "np.ndarray | None") -> "np.ndarray | None":
    """! @brief WORKxWORK uint8 BGR (feedback samples / benches only; scoring never resizes)."""
    if img is None:
        return None
    try:
        return cv2.resize(_bgr3(img), (WORK, WORK), interpolation=cv2.INTER_AREA)
    except Exception:
        return None


def _pad8(img: "np.ndarray") -> "np.ndarray":
    h, w = img.shape[:2]
    ph, pw = (-h) % STRIDE, (-w) % STRIDE
    if ph or pw:
        img = cv2.copyMakeBorder(img, 0, ph, 0, pw, cv2.BORDER_REFLECT_101)
    return img


def cell_mask(px_mask: "np.ndarray") -> "np.ndarray":
    """! @brief Per-pixel change mask [H,W] (0..1) -> per-cell mean [H/8,W/8] float32."""
    m = _pad8(px_mask.astype(np.float32)[:, :, None])[:, :, 0]
    h, w = m.shape
    return m.reshape(h // STRIDE, STRIDE, w // STRIDE, STRIDE).mean(axis=(1, 3))


def _similarity(rs, is_, s_ref, s_img, min_inliers):
    """ORB + RANSAC similarity transform mapping is_ (scaled s_img) onto rs (scaled s_ref); (M, inliers)."""
    try:
        orb = cv2.ORB_create(2000)
        k1, d1 = orb.detectAndCompute(cv2.cvtColor(rs, cv2.COLOR_BGR2GRAY), None)
        k2, d2 = orb.detectAndCompute(cv2.cvtColor(is_, cv2.COLOR_BGR2GRAY), None)
        if d1 is None or d2 is None or len(k1) < min_inliers or len(k2) < min_inliers:
            return None, 0
        m = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d2, d1), key=lambda x: x.distance)[:500]
        if len(m) < min_inliers:
            return None, 0
        src = np.float32([k2[x.queryIdx].pt for x in m]) / s_img
        dst = np.float32([k1[x.trainIdx].pt for x in m]) / s_ref
        A, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
        n = int(inl.sum()) if inl is not None else 0
        return (A if A is not None and n >= min_inliers else None), n
    except Exception:
        return None, 0


def align(ref: "np.ndarray", img: "np.ndarray", min_inliers: int = 12):
    """!
    @brief Warp `img` onto `ref`'s frame with a similarity transform (ORB +
           RANSAC on a <=1024-px working copy, transform scaled back so the
           warp itself is full resolution). A mirrored copy is tried too and
           wins when it aligns better. Same framing (aspect within 2%) with
           too few features falls back to a plain resize.
    @return (warped uint8 [H,W,3], overlap bool [H,W]) or None: not the same picture.
    """
    ref, img = _bgr3(ref), _bgr3(img)
    H, W = ref.shape[:2]; h, w = img.shape[:2]
    same_frame = abs(W / H - w / h) <= 0.02 * (W / H)

    def small(x):
        s = 1024.0 / max(x.shape[:2])
        return (cv2.resize(x, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else x), min(s, 1.0)
    rs, s_ref = small(ref); is_, s_img = small(img)
    M, n = _similarity(rs, is_, s_ref, s_img, min_inliers)
    Mf, nf = _similarity(rs, is_[:, ::-1], s_ref, s_img, min_inliers)
    if Mf is not None and nf > n:
        M, img = Mf, np.ascontiguousarray(img[:, ::-1])
    if M is None:
        if not same_frame:
            return None
        warped = img if (h, w) == (H, W) else cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA if h * w > H * W else cv2.INTER_LINEAR)
        return warped, np.ones((H, W), bool)
    warped = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    ov = cv2.warpAffine(np.full((h, w), 255, np.uint8), M, (W, H), flags=cv2.INTER_NEAREST) > 0
    if ov.mean() < 0.05:
        return None
    return warped, ov


def pair_score(change_map: "np.ndarray", overlap: "np.ndarray | None") -> float:
    """! @brief Unchanged fraction over the overlap x the overlap's share of
    the reference: identical 60% crop -> 0.6; a faint stamp on 2% -> ~0.98."""
    if overlap is None:
        return float(1.0 - change_map.mean())
    ov = cell_mask(overlap) >= 0.5
    if not ov.any():
        return 0.0
    return float((1.0 - change_map[ov].mean()) * ov.mean())


def encode_pair(img_a: "np.ndarray", img_b: "np.ndarray") -> "bytes | None":
    """! @brief Serialize a feedback pair (two WORKxWORK uint8 BGR) for dup_cnn_samples."""
    a, b = _to_work_u8(img_a), _to_work_u8(img_b)
    if a is None or b is None:
        return None
    buf = io.BytesIO()
    np.savez_compressed(buf, a=a, b=b)
    return buf.getvalue()


if _HAVE_TORCH:
    def _tensor(x, device: str) -> "torch.Tensor":
        """[n,H,W,3] uint8 HWC (or [n,3,H,W] float32) numpy -> float 0..1 channels_last tensor."""
        t = torch.from_numpy(np.ascontiguousarray(x))
        if device != "cpu":
            t = t.pin_memory()
        t = t.to(device, non_blocking=True)
        if t.dtype == torch.uint8:
            t = t.permute(0, 3, 1, 2).float().div_(255.0)
        return t.contiguous(memory_format=torch.channels_last)

    class _Encoder(nn.Module):
        """One image -> per-cell feature grid [C, H/8, W/8]. Lossless stem."""

        def __init__(self, width_mult: float, depth: int) -> None:
            super().__init__()
            C = _ch(width_mult)
            self.stem = nn.Sequential(nn.PixelUnshuffle(STRIDE), nn.Conv2d(3 * STRIDE * STRIDE, C, 1),
                                      nn.BatchNorm2d(C), nn.ReLU(inplace=True))
            blocks = []
            for d in _dilations(depth):
                blocks += [nn.Conv2d(C, C, 3, padding=d, dilation=d), nn.BatchNorm2d(C), nn.ReLU(inplace=True)]
            self.body = nn.Sequential(*blocks)
            self.dim = C

        def forward(self, x):
            return self.body(self.stem(x))

    class _Head(nn.Module):
        """Two feature grids -> change logit per cell [H/8, W/8]."""

        def __init__(self, C: int) -> None:
            super().__init__()
            self.net = nn.Sequential(nn.Conv2d(2 * C, C, 3, padding=1), nn.BatchNorm2d(C), nn.ReLU(inplace=True),
                                     nn.Conv2d(C, 1, 1))

        def forward(self, fa, fb):
            return self.net(torch.cat([(fa - fb).abs(), fa * fb], dim=1)).squeeze(1)

    class _ChangeNet(nn.Module):
        def __init__(self, width_mult: float, depth: int) -> None:
            super().__init__()
            self.enc = _Encoder(width_mult, depth)
            self.head = _Head(self.enc.dim)

        def forward(self, a, b):
            return self.head(self.enc(a), self.enc(b))


class DupCNN:
    """! @brief Change-net wrapper with a size knob and safe fallbacks (no torch -> neutral no-ops)."""

    def __init__(self, width_mult: float = 1.0, depth: int = 1, size: str = "") -> None:
        self.width_mult: float = min(WIDTH_MAX, max(WIDTH_MIN, float(width_mult)))
        self.depth: int = max(1, int(depth))
        self.size: str = size
        self.trained: bool = False
        self.error: str = ""
        self.net = _ChangeNet(self.width_mult, self.depth) if _HAVE_TORCH else None

    @classmethod
    def sized(cls, size: str, sizes: "dict | None" = None) -> "DupCNN":
        sp = size_spec(size, sizes)
        return cls(sp["width"], sp["depth"], size=str(size).lower())

    @property
    def available(self) -> bool:
        return _HAVE_TORCH and self.net is not None

    @property
    def params(self) -> int:
        return sum(p.numel() for p in self.net.parameters()) if self.available else 0

    def _raw(self):
        return getattr(self.net, "_orig_mod", self.net)

    @classmethod
    def load(cls, path: str, width_mult: float = 1.0, depth: int = 1) -> "DupCNN":
        """! @brief Load a checkpoint (carries width/depth/size); untrained fallback on failure."""
        m = cls(width_mult, depth)
        if not _HAVE_TORCH:
            return m
        try:
            ckpt = torch.load(path, map_location="cpu")
            if ckpt.get("arch") != "changenet":
                raise ValueError("not a change-net checkpoint (old siamese weights?)")
            m.width_mult = float(ckpt.get("width_mult", width_mult))
            m.depth = int(ckpt.get("depth", 1))
            m.size = str(ckpt.get("size", ""))
            m.net = _ChangeNet(m.width_mult, m.depth)
            m.net.load_state_dict(ckpt["state_dict"])
            m.net.eval()
            m.trained = True
        except Exception as e:
            m.trained = False
            m.error = f"{type(e).__name__}: {e}"
        return m

    def compile(self) -> bool:
        if not self.available:
            return False
        try:
            self.net = torch.compile(self.net)
            return True
        except Exception:
            return False

    def save(self, path: str) -> bool:
        if not self.available:
            return False
        try:
            tmp = path + ".tmp"
            torch.save({"arch": "changenet", "state_dict": self._raw().state_dict(),
                        "width_mult": self.width_mult, "depth": self.depth, "size": self.size,
                        "stride": STRIDE, "saved": time.time()}, tmp)
            os.replace(tmp, path)
            return True
        except Exception:
            return False

    def fit_batches(self, batches, lr: float = 1e-3, device: str = "cpu",
                    _opt_holder: dict = None, amp: "str | bool" = "", micro: int = 0) -> "float | None":
        """!
        @brief One pass over (a, b, m) numpy batches: a, b uint8 [n,S,S,3]
               aligned pairs, m float32 [n,S/8,S/8] target change per cell
               (0 = identical, 1 = different, in between for partial edits).
               Per-cell BCE. `micro`: GPU micro-batch with gradient
               accumulation (same maths as the full batch, memory of the slice).
        @return Mean loss of the pass, or None when torch is missing.
        """
        if not self.available:
            return None
        self.net.to(device, memory_format=torch.channels_last).train()
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
        loss_fn = nn.BCEWithLogitsLoss()
        total, n = 0.0, 0
        mode = "bf16" if amp is True else ("" if not amp else str(amp).lower())
        use_amp = mode in ("bf16", "fp16") and device != "cpu"
        scaler = None
        if use_amp and mode == "fp16":
            scaler = holder.get("scaler")
            if scaler is None:
                try:
                    scaler = torch.amp.GradScaler("cuda")
                except Exception:
                    scaler = torch.cuda.amp.GradScaler()
                holder["scaler"] = scaler
        for a, b, m in batches:
            bs = len(m)
            step = int(micro) if micro and int(micro) < bs else bs
            opt.zero_grad(set_to_none=True)
            for j in range(0, bs, step):
                ta, tb = _tensor(a[j:j + step], device), _tensor(b[j:j + step], device)
                tm = torch.as_tensor(np.asarray(m[j:j + step], np.float32)).to(device, non_blocking=True)
                with torch.autocast(device_type="cuda", dtype=torch.float16 if mode == "fp16" else torch.bfloat16,
                                    enabled=use_amp):
                    logit = self.net(ta, tb)
                loss = loss_fn(logit.float(), tm) * (len(tm) / bs)
                (scaler.scale(loss) if scaler is not None else loss).backward()
                total += float(loss.item()) * bs; n += len(tm)
            if scaler is not None:
                scaler.step(opt); scaler.update()
            else:
                opt.step()
        self.net.eval()
        self.trained = self.trained or n > 0
        return total / n if n else None

    # ── inference ────────────────────────────────────────────────────────────
    def encode(self, img: "np.ndarray", device: str = "cpu", max_pixels: int = 16_000_000) -> "torch.Tensor":
        """!
        @brief Feature grid [C, H/8, W/8] of one BGR image at native resolution.
               Above `max_pixels` the image is encoded in horizontal strips with
               margin_px(depth) of overlap, margins cropped off — the features
               are identical to a single pass, only memory is bounded.
        """
        self.net.to(device, memory_format=torch.channels_last).eval()
        img = _pad8(_bgr3(img))
        H, W = img.shape[:2]
        enc = self._raw().enc
        with torch.no_grad():
            if H * W <= max_pixels:
                return enc(_tensor(img[None], device))[0]
            mg = margin_px(self.depth)
            rows = max(STRIDE, (max_pixels // W) // STRIDE * STRIDE)
            parts = []
            for y in range(0, H, rows):
                y0, y1 = max(0, y - mg), min(H, y + rows + mg)
                f = enc(_tensor(img[None, y0:y1], device))[0]
                c0 = (y - y0) // STRIDE
                parts.append(f[:, c0:c0 + min(rows, H - y) // STRIDE])
            return torch.cat(parts, dim=1)

    def compare(self, fa: "torch.Tensor", fb: "torch.Tensor") -> "np.ndarray":
        """! @brief Change probabilities [H/8, W/8] between two feature grids."""
        with torch.no_grad():
            return torch.sigmoid(self._raw().head(fa[None], fb[None]))[0].float().cpu().numpy()

    def predict_maps(self, a, b, device: str = "cpu") -> "np.ndarray":
        """! @brief Change maps [n,S/8,S/8] for prepared aligned batches (training eval)."""
        self.net.to(device, memory_format=torch.channels_last).eval()
        with torch.no_grad():
            return torch.sigmoid(self.net(_tensor(a, device), _tensor(b, device))).float().cpu().numpy()

    def predict_batch(self, a, b, device: str = "cpu") -> "np.ndarray":
        """! @brief Unchanged fraction per prepared aligned pair (training eval)."""
        return 1.0 - self.predict_maps(a, b, device).mean(axis=(1, 2))

    def score_group(self, imgs: "list", device: str = "cpu", max_pixels: int = 16_000_000,
                    want_maps: bool = False):
        """!
        @brief NxN matrix of pair scores for a group of BGR images at native
               resolution. Encode once, compare many: the largest image is the
               reference, every other member is aligned onto it and encoded
               once; each pair is one head pass on feature grids. Members that
               cannot be aligned to the reference are scored among themselves
               with their own reference (recursively). Diagonal = 1.
        @return (matrix float32 [N,N], maps) — maps is {(i,j): change map in
                the frame of i} when want_maps, else None.
        """
        N = len(imgs)
        S = np.eye(N, dtype=np.float32)
        maps = {} if want_maps else None
        if not (self.available and self.trained) or N < 2:
            return S, maps

        def rec(idx):
            if len(idx) < 2:
                return
            ref = max(idx, key=lambda i: imgs[i].shape[0] * imgs[i].shape[1])
            ref_img = _bgr3(imgs[ref])
            aligned, orphans = {ref: (ref_img, None)}, []
            for i in idx:
                if i == ref:
                    continue
                r = align(ref_img, imgs[i])
                if r is None:
                    orphans.append(i)
                else:
                    aligned[i] = r
            feats = {i: self.encode(a, device, max_pixels) for i, (a, _) in aligned.items()}
            keys = list(aligned)
            for p in range(len(keys)):
                for q in range(p + 1, len(keys)):
                    i, j = keys[p], keys[q]
                    cm = self.compare(feats[i], feats[j])
                    ov = aligned[i][1] if aligned[j][1] is None else (
                        aligned[j][1] if aligned[i][1] is None else aligned[i][1] & aligned[j][1])
                    S[i, j] = S[j, i] = pair_score(cm, ov)
                    if maps is not None:
                        maps[(i, j)] = maps[(j, i)] = cm
            del feats
            rec(orphans)

        rec(list(range(N)))
        return S, maps

    def predict(self, img_a: "np.ndarray", img_b: "np.ndarray", device: str = "cpu") -> "float | None":
        """! @brief Score one pair (align + encode + compare); None when untrained."""
        if not (self.available and self.trained):
            return None
        try:
            return float(self.score_group([img_a, img_b], device)[0][0, 1])
        except Exception:
            return None

    def bench(self, device: str = "cpu", batch: int = 64, reps: int = 5) -> "dict":
        """! @brief params, ms per aligned WORKxWORK pair at batch 1 and at `batch`, train memory at `batch`."""
        if not self.available:
            return {}
        gpu = device != "cpu" and torch.cuda.is_available()
        dev = device if gpu else "cpu"
        sync = (lambda: torch.cuda.synchronize(dev)) if gpu else (lambda: None)
        net = self.net.to(dev, memory_format=torch.channels_last).eval()
        out = {"params": self.params, "device": dev, "batch": batch, "side": WORK}
        with torch.no_grad():
            for n, key in ((1, "ms_per_pair_b1"), (batch, "ms_per_pair_batch")):
                a = torch.rand(n, 3, WORK, WORK, device=dev).contiguous(memory_format=torch.channels_last)
                b = torch.rand_like(a)
                net(a, b); sync()
                t = time.perf_counter()
                for _ in range(reps):
                    net(a, b)
                sync()
                out[key] = round((time.perf_counter() - t) / reps / n * 1000, 3)
        if gpu:
            torch.cuda.reset_peak_memory_stats(dev)
            net.train()
            a = torch.rand(batch, 3, WORK, WORK, device=dev).contiguous(memory_format=torch.channels_last)
            b = torch.rand_like(a)
            m = torch.rand(batch, WORK // STRIDE, WORK // STRIDE, device=dev)
            nn.BCEWithLogitsLoss()(net(a, b), m).backward()
            net.zero_grad(set_to_none=True); sync()
            out["train_mem_mb"] = round(torch.cuda.max_memory_allocated(dev) / 2**20)
            net.eval()
        return out