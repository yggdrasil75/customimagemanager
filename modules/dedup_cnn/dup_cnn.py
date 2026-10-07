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
different picture. Very large images are encoded in overlapping strips -
a memory bound only, never a resolution cut.

Trained on native-resolution crops with a per-pixel change mask target
(modules/dedup_train). Sizes scale width (cells' channels) and depth (body
blocks); checkpoints carry their own width/depth.

HEURDU 1.0 (animation, <= ANIM_MAX_FRAMES frames): the same encoder and
head, plus a temporal block - a residual Conv3d (3,1,1) over each clip's
stacked feature grids, zero-initialised so a 0.9 checkpoint upgraded to 1.0
scores exactly as before until it is trained. A clip pair is aligned once
(one transform, every frame warped the same), every frame of each clip is
encoded once, the head fills a frame x frame score matrix and closed-end DTW
(modules/dedup/seq_align) turns it into one score: dropped / duplicated
frames cost nothing, a trimmed animation scores its shared fraction. 0.9
checkpoints ("changenet") run the same path without the temporal block, so
the released model keeps working on animations until 1.0 is trained.
"""

import io
import numpy as np
import os
import time

from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

torch, _HAVE_TORCH = optional_import("torch")
nn = torch.nn if _HAVE_TORCH else None

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
ANIM_MAX_FRAMES: int = 30          # HEURDU's animation limit; longer clips are HEURDUV's (dedup_cnn_video)
ARCH_V09, ARCH_V10 = "changenet", "changenet-t"


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


def count_params(width_mult: float, depth: int = 1, temporal: bool = False) -> int:
    """! @brief Parameter count of a size without building it (no torch needed)."""
    C, d = _ch(width_mult), max(1, int(depth))
    stem = 3 * STRIDE * STRIDE * C + C + 2 * C
    body = d * (9 * C * C + C + 2 * C)
    head = 2 * C * C * 9 + C + 2 * C + C + 1
    return stem + body + head + ((3 * C * C + C) if temporal else 0)


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
    m = px_mask.astype(np.float32)
    ph, pw = (-m.shape[0]) % STRIDE, (-m.shape[1]) % STRIDE
    if ph or pw:                       # cv2.copyMakeBorder drops a singleton channel, so pad in numpy
        m = np.pad(m, ((0, ph), (0, pw)), mode="edge")
    h, w = m.shape
    return m.reshape(h // STRIDE, STRIDE, w // STRIDE, STRIDE).mean(axis=(1, 3))


def _similarity(rs, is_, s_ref, s_img, min_inliers):
    """! @brief ORB + RANSAC similarity transform mapping is_ (scaled s_img) onto rs (scaled s_ref); (M, inliers)."""
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


def align_params(ref: "np.ndarray", img: "np.ndarray", min_inliers: int = 12):
    """!
    @brief The transform align() applies, without applying it: lets a clip
           compute it once (on one frame pair) and warp every frame the same.
    @return None (not the same picture) or (M | None, flipped, (W, H), overlap bool [H,W]);
            M None = same framing, plain resize.
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
    flip = False
    if Mf is not None and nf > n:
        M, flip = Mf, True
    if M is None:
        if not same_frame:
            return None
        return None, False, (W, H), np.ones((H, W), bool)
    ov = cv2.warpAffine(np.full((h, w), 255, np.uint8), M, (W, H), flags=cv2.INTER_NEAREST) > 0
    if ov.mean() < 0.05:
        return None
    return M, flip, (W, H), ov


def apply_align(img: "np.ndarray", params) -> "np.ndarray":
    """! @brief Warp `img` (any frame of the clip align_params saw) into the reference frame."""
    M, flip, (W, H), _ov = params
    img = _bgr3(img)
    if flip:
        img = np.ascontiguousarray(img[:, ::-1])
    h, w = img.shape[:2]
    if M is None:
        return img if (h, w) == (H, W) else cv2.resize(
            img, (W, H), interpolation=cv2.INTER_AREA if h * w > H * W else cv2.INTER_LINEAR)
    return cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)


def align(ref: "np.ndarray", img: "np.ndarray", min_inliers: int = 12):
    """!
    @brief Warp `img` onto `ref`'s frame with a similarity transform (ORB +
           RANSAC on a <=1024-px working copy, transform scaled back so the
           warp itself is full resolution). A mirrored copy is tried too and
           wins when it aligns better. Same framing (aspect within 2%) with
           too few features falls back to a plain resize.
    @return (warped uint8 [H,W,3], overlap bool [H,W]) or None: not the same picture.
    """
    p = align_params(ref, img, min_inliers)
    if p is None:
        return None
    return apply_align(img, p), p[3]


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
        """! @brief [n,H,W,3] uint8 HWC (or [n,3,H,W] float32) numpy -> float 0..1 channels_last tensor."""
        t = torch.from_numpy(np.ascontiguousarray(x))
        if device != "cpu":
            t = t.pin_memory()
        t = t.to(device, non_blocking=True)
        if t.dtype == torch.uint8:
            t = t.permute(0, 3, 1, 2).float().div_(255.0)
        return t.contiguous(memory_format=torch.channels_last)

    def _clip_tensor(x, device: str) -> "torch.Tensor":
        """! @brief [n,T,H,W,3] uint8 numpy -> float 0..1 [n,T,3,H,W] tensor."""
        t = torch.from_numpy(np.ascontiguousarray(x)).to(device, non_blocking=True)
        return t.permute(0, 1, 4, 2, 3).float().div_(255.0).contiguous()

    class _Encoder(nn.Module):
        """! @brief One image -> per-cell feature grid [C, H/8, W/8]. Lossless stem."""

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
        """! @brief Two feature grids -> change logit per cell [H/8, W/8]."""

        def __init__(self, C: int) -> None:
            super().__init__()
            self.net = nn.Sequential(nn.Conv2d(2 * C, C, 3, padding=1), nn.BatchNorm2d(C), nn.ReLU(inplace=True),
                                     nn.Conv2d(C, 1, 1))

        def forward(self, fa, fb):
            return self.net(torch.cat([(fa - fb).abs(), fa * fb], dim=1)).squeeze(1)

    class _Temporal(nn.Module):
        """! @brief HEURDU 1.0: residual Conv3d (3,1,1) across a clip's frames, per cell.
        Zero-initialised: identity until trained, so 0.9 weights upgrade losslessly."""

        def __init__(self, C: int) -> None:
            super().__init__()
            self.conv = nn.Conv3d(C, C, (3, 1, 1), padding=(1, 0, 0))
            nn.init.zeros_(self.conv.weight)
            nn.init.zeros_(self.conv.bias)

        def forward(self, f):
            """! @brief f [N, T, C, h, w] -> same shape."""
            x = f.permute(0, 2, 1, 3, 4).contiguous()
            return f + self.conv(x).permute(0, 2, 1, 3, 4)

    class _ChangeNet(nn.Module):
        def __init__(self, width_mult: float, depth: int, temporal: bool = False) -> None:
            super().__init__()
            self.enc = _Encoder(width_mult, depth)
            self.head = _Head(self.enc.dim)
            if temporal:
                self.temporal = _Temporal(self.enc.dim)

        def forward(self, a, b):
            return self.head(self.enc(a), self.enc(b))

        def forward_clips(self, a, b):
            """! @brief a, b [N, T, 3, S, S] frame-aligned clips -> change logits [N, T, S/8, S/8]."""
            N, T = a.shape[:2]
            fa = self.enc(a.flatten(0, 1).contiguous(memory_format=torch.channels_last))
            fb = self.enc(b.flatten(0, 1).contiguous(memory_format=torch.channels_last))
            fa, fb = fa.view(N, T, *fa.shape[1:]), fb.view(N, T, *fb.shape[1:])
            if hasattr(self, "temporal"):
                fa, fb = self.temporal(fa), self.temporal(fb)
            out = self.head(fa.flatten(0, 1), fb.flatten(0, 1))
            return out.view(N, T, *out.shape[1:])


class DupCNN:
    """! @brief Change-net wrapper with a size knob and safe fallbacks (no torch -> neutral no-ops)."""

    def __init__(self, width_mult: float = 1.0, depth: int = 1, size: str = "", temporal: bool = False) -> None:
        self.width_mult: float = min(WIDTH_MAX, max(WIDTH_MIN, float(width_mult)))
        self.depth: int = max(1, int(depth))
        self.size: str = size
        self.trained: bool = False
        self.error: str = ""
        self.net = _ChangeNet(self.width_mult, self.depth, temporal) if _HAVE_TORCH else None

    @property
    def temporal(self) -> bool:
        """! @brief True for a HEURDU 1.0 net (has the temporal block)."""
        return bool(self.available and hasattr(self._raw(), "temporal"))

    @property
    def version(self) -> str:
        return "1.0" if self.temporal else "0.9"

    def upgrade(self) -> "DupCNN":
        """! @brief 0.9 -> 1.0 in place: add the zero-initialised temporal block
               (scores are unchanged until it is trained). No-op on 1.0."""
        if self.available and not self.temporal:
            raw = self._raw()
            raw.temporal = _Temporal(raw.enc.dim).to(next(raw.parameters()).device)
            self.net = raw
        return self

    @classmethod
    def sized(cls, size: str, sizes: "dict | None" = None, temporal: bool = False) -> "DupCNN":
        sp = size_spec(size, sizes)
        return cls(sp["width"], sp["depth"], size=str(size).lower(), temporal=temporal)

    @property
    def available(self) -> bool:
        return _HAVE_TORCH and self.net is not None

    @property
    def params(self) -> int:
        return sum(p.numel() for p in self.net.parameters()) if self.available else 0

    def _raw(self):
        return getattr(self.net, "_orig_mod", self.net)

    def _place(self, device: str):
        """! @brief Move to device; 2D convs channels_last (a 1.0 net's Conv3d can't be)."""
        net = self.net.to(device)
        for mod in net.modules():
            if isinstance(mod, nn.Conv2d):
                mod.to(memory_format=torch.channels_last)
        return net

    @classmethod
    def load(cls, path: str, width_mult: float = 1.0, depth: int = 1) -> "DupCNN":
        """! @brief Load a checkpoint (carries width/depth/size); untrained fallback on failure."""
        m = cls(width_mult, depth)
        if not _HAVE_TORCH:
            return m
        try:
            ckpt = torch.load(path, map_location="cpu")
            if ckpt.get("arch") not in (ARCH_V09, ARCH_V10):
                raise ValueError("not a change-net checkpoint (old siamese weights?)")
            m.width_mult = float(ckpt.get("width_mult", width_mult))
            m.depth = int(ckpt.get("depth", 1))
            m.size = str(ckpt.get("size", ""))
            m.net = _ChangeNet(m.width_mult, m.depth, ckpt.get("arch") == ARCH_V10)
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
            torch.save({"arch": ARCH_V10 if self.temporal else ARCH_V09, "heurdu": self.version,
                        "state_dict": self._raw().state_dict(),
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
               Clips (HEURDU 1.0): a, b uint8 [n,T,S,S,3] frame-aligned,
               m [n,T,S/8,S/8]; the temporal block trains with the rest.
               Per-cell BCE. `micro`: GPU micro-batch with gradient
               accumulation (same maths as the full batch, memory of the slice).
        @return Mean loss of the pass, or None when torch is missing.
        """
        if not self.available:
            return None
        self._place(device).train()
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
                clip = np.ndim(a) == 5
                ta, tb = (_clip_tensor if clip else _tensor)(a[j:j + step], device), \
                    (_clip_tensor if clip else _tensor)(b[j:j + step], device)
                tm = torch.as_tensor(np.asarray(m[j:j + step], np.float32)).to(device, non_blocking=True)
                with torch.autocast(device_type="cuda", dtype=torch.float16 if mode == "fp16" else torch.bfloat16,
                                    enabled=use_amp):
                    logit = self._raw().forward_clips(ta, tb) if clip else self.net(ta, tb)
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

    # -- inference ------------------------------------------------------------
    def encode(self, img: "np.ndarray", device: str = "cpu", max_pixels: int = 16_000_000) -> "torch.Tensor":
        """!
        @brief Feature grid [C, H/8, W/8] of one BGR image at native resolution.
               Above `max_pixels` the image is encoded in horizontal strips with
               margin_px(depth) of overlap, margins cropped off - the features
               are identical to a single pass, only memory is bounded.
        """
        self._place(device).eval()
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

    def compare_many(self, fas: "list", fbs: "list", per_pass: int = 1) -> "np.ndarray":
        """! @brief Change maps [n, H/8, W/8] for same-shaped feature-grid pairs,
               `per_pass` pairs per head call (batched; BN is in eval so the
               result equals one-at-a-time)."""
        head = self._raw().head
        out = []
        with torch.no_grad():
            for s in range(0, len(fas), max(1, per_pass)):
                a = torch.stack(fas[s:s + per_pass]); b = torch.stack(fbs[s:s + per_pass])
                out.append(torch.sigmoid(head(a, b)).float().cpu().numpy())
                del a, b
        return np.concatenate(out, axis=0) if out else np.empty((0,), np.float32)

    def predict_maps(self, a, b, device: str = "cpu") -> "np.ndarray":
        """! @brief Change maps [n,S/8,S/8] for prepared aligned batches (training eval)."""
        self._place(device).eval()
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
        @return (matrix float32 [N,N], maps) - maps is {(i,j): change map in
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
            pairs = [(keys[p], keys[q]) for p in range(len(keys)) for q in range(p + 1, len(keys))]
            # Every member is warped into the reference frame, so all grids
            # share a shape: batch the head over as many pairs as fit in the
            # same pixel budget that bounds the encoder.
            H8, W8 = feats[ref].shape[-2:]
            per_pass = max(1, max_pixels // max(1, H8 * W8 * STRIDE * STRIDE))
            for s0 in range(0, len(pairs), per_pass):
                chunk = pairs[s0:s0 + per_pass]
                cms = self.compare_many([feats[i] for i, _ in chunk], [feats[j] for _, j in chunk], per_pass)
                for (i, j), cm in zip(chunk, cms):
                    ov = aligned[i][1] if aligned[j][1] is None else (
                        aligned[j][1] if aligned[i][1] is None else aligned[i][1] & aligned[j][1])
                    S[i, j] = S[j, i] = pair_score(cm, ov)
                    if maps is not None:
                        maps[(i, j)] = maps[(j, i)] = cm
            del feats
            rec(orphans)

        rec(list(range(N)))
        return S, maps

    def encode_clip(self, frames: "list", device: str = "cpu", max_pixels: int = 16_000_000) -> "list":
        """! @brief Feature grids of every frame of one clip (same size frames);
               the temporal block (1.0) mixes neighbouring frames per cell."""
        feats = [self.encode(f, device, max_pixels) for f in frames]
        if self.temporal and len(feats) > 1:
            with torch.no_grad():
                st = self._raw().temporal(torch.stack(feats)[None])[0]
            feats = list(st.unbind(0))
        return feats

    def score_animation(self, frames_a: "list", frames_b: "list", device: str = "cpu",
                        max_pixels: int = 16_000_000, clip_pixels: int = 64_000_000,
                        want_matrix: bool = False):
        """!
        @brief HEURDU animation score for two clips of <= ANIM_MAX_FRAMES BGR frames.
               The clip with the larger frames is the reference; one similarity
               transform (from the middle frames, else the first) warps every
               frame of the other; every frame is encoded once; the head fills
               the frame x frame pair_score matrix; DTW turns it into the shared
               fraction of the longer clip. Unalignable -> 0.0.
               clip_pixels bounds T x H x W per clip (frames are scaled down past
               it - a memory bound for long native-resolution animations).
        @return score (float), or (score, matrix [Ta, Tb]) when want_matrix; None untrained.
        """
        from modules.dedup.seq_align import dtw_score, resample_idx
        if not (self.available and self.trained):
            return None
        fa = [_bgr3(f) for f in frames_a if f is not None]
        fb = [_bgr3(f) for f in frames_b if f is not None]
        if not fa or not fb:
            return None
        fa = [fa[i] for i in resample_idx(len(fa), ANIM_MAX_FRAMES)]
        fb = [fb[i] for i in resample_idx(len(fb), ANIM_MAX_FRAMES)]
        swap = fb[0].shape[0] * fb[0].shape[1] > fa[0].shape[0] * fa[0].shape[1]
        if swap:
            fa, fb = fb, fa

        def bound(fr):
            h, w = fr[0].shape[:2]
            s = (clip_pixels / float(len(fr) * h * w)) ** 0.5
            if s >= 1:
                return fr
            size = (max(STRIDE, int(w * s)), max(STRIDE, int(h * s)))
            return [cv2.resize(x, size, interpolation=cv2.INTER_AREA) for x in fr]
        fa, fb = bound(fa), bound(fb)
        prm = align_params(fa[len(fa) // 2], fb[len(fb) // 2]) or align_params(fa[0], fb[0])
        if prm is None:
            return (0.0, np.zeros((len(fa), len(fb)), np.float32)) if want_matrix else 0.0
        ov = prm[3]
        wb = [apply_align(x, prm) for x in fb]
        ea, eb = self.encode_clip(fa, device, max_pixels), self.encode_clip(wb, device, max_pixels)
        pairs = [(i, j) for i in range(len(ea)) for j in range(len(eb))]
        H8, W8 = ea[0].shape[-2:]
        per = max(1, max_pixels // max(1, H8 * W8 * STRIDE * STRIDE))
        S = np.zeros((len(ea), len(eb)), np.float32)
        for s0 in range(0, len(pairs), per):
            ch = pairs[s0:s0 + per]
            cms = self.compare_many([ea[i] for i, _ in ch], [eb[j] for _, j in ch], per)
            for (i, j), cm in zip(ch, cms):
                S[i, j] = pair_score(cm, ov)
        sc = dtw_score(1.0 - S)
        if swap:
            S = S.T
        return (sc, S) if want_matrix else sc

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
        net = self._place(dev).eval()
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