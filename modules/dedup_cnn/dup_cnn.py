"""!
@brief Siamese CNN duplicate classifier, trained from user merge/exclude feedback.

Sits alongside the logistic DuplicateClassifier in dup_heuristics: this is the
stronger model when a trained checkpoint and torch are both present; the app
falls back to the logistic model otherwise. Samples are stored as small resized
image-pair tensors (not 9-float feature vectors), so this has its own store and
cannot share dup_heuristics' dup_samples table.
"""

import io
import numpy as np
import os

from optional_deps import optional_import
cv2, _HAVE_CV2 = optional_import("cv2")

try:
    import torch
    from torch import nn
    _HAVE_TORCH = True
except Exception:
    _HAVE_TORCH = False

WORK: int = 128
WIDTH_MIN: float = 0.125
WIDTH_MAX: float = 8.0

# Named size series (nano..xxl). width scales the channel counts [16,32,64,128];
# depth is conv blocks per stage (1 = the original 4-conv tower). Checkpoints
# store their own width/depth, so any size loads regardless of the setting.
SIZES: "dict[str, dict]" = {
    "nano":   {"width": 0.25, "depth": 1},
    "small":  {"width": 0.5,  "depth": 1},
    "medium": {"width": 1.0,  "depth": 2},
    "large":  {"width": 2.0,  "depth": 2},
    "xl":     {"width": 3.0,  "depth": 3},
    "xxl":    {"width": 4.0,  "depth": 3},
}
SIZE_ORDER = list(SIZES)


def parse_sizes(text: str) -> "dict[str, dict]":
    """!
    @brief User size table from the dup_cnn_sizes setting: one "name width depth"
           per line (separators: space, comma, colon). Empty/invalid -> the defaults.
    """
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
    """! @brief The inverse of parse_sizes, for the settings textarea."""
    return "\n".join(f"{k} {v['width']} {v['depth']}" for k, v in (sizes or SIZES).items())


def size_spec(size: str, sizes: "dict | None" = None) -> "dict":
    """! @brief {width, depth} for a size name in `sizes` (default table); unknown -> medium."""
    tbl = sizes or SIZES
    return dict(tbl.get(str(size).lower()) or tbl.get("medium") or next(iter(tbl.values())))


def count_params(width_mult: float, depth: int = 1) -> int:
    """! @brief Parameter count of a size without building it (no torch needed)."""
    n, cin = 0, 3
    for c in _channels(width_mult):
        n += cin * c * 9 + c + 2 * c
        n += (max(1, int(depth)) - 1) * (c * c * 9 + c + 2 * c)
        cin = c
    return n + 2 * cin * cin + cin + cin + 1

def _to_work_bgr(img: "np.ndarray | None") -> "np.ndarray | None":
    """!
    @brief Resize any BGR/gray array to a WORKxWORKx3 float32 tensor in 0..1.
    @return CHW float32 array, or None if the image is unusable.
    """
    if img is None:
        return None
    try:
        if img.ndim == 2:
            img = np.repeat(img[:, :, None], 3, axis=2)
        bgr = img[:, :, :3]
        r = cv2.resize(bgr, (WORK, WORK), interpolation=cv2.INTER_AREA)
        return (r.astype(np.float32) / 255.0).transpose(2, 0, 1)
    except Exception:
        return None

def encode_pair(img_a: "np.ndarray", img_b: "np.ndarray") -> "bytes | None":
    """!
    @brief Serialize an image pair to the stored training sample (two CHW tensors).
    @return npz bytes of arrays 'a' and 'b', or None if either image is unusable.
    """
    a = _to_work_bgr(img_a)
    b = _to_work_bgr(img_b)
    if a is None or b is None:
        return None
    buf = io.BytesIO()
    np.savez_compressed(buf, a=a, b=b)
    return buf.getvalue()

def _channels(width_mult: float) -> "list[int]":
    base = [16, 32, 64, 128]
    return [max(4, int(round(c * width_mult))) for c in base]

if _HAVE_TORCH:
    class _Encoder(nn.Module):
        """! @brief Shared conv tower mapping one WORKxWORK BGR image to an embedding."""

        def __init__(self, width_mult: float, depth: int = 1) -> None:
            super().__init__()
            layers, cin = [], 3
            for c in _channels(width_mult):
                layers += [nn.Conv2d(cin, c, 3, 2, 1), nn.BatchNorm2d(c), nn.ReLU(inplace=True)]
                for _ in range(max(1, int(depth)) - 1):
                    layers += [nn.Conv2d(c, c, 3, 1, 1), nn.BatchNorm2d(c), nn.ReLU(inplace=True)]
                cin = c
            layers += [nn.AdaptiveAvgPool2d(1), nn.Flatten()]
            self.net = nn.Sequential(*layers)
            self.embed_dim = cin

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            return self.net(x)

    class _SiameseNet(nn.Module):
        """! @brief Encode both images with a shared tower, classify the pair from |a-b| and a*b."""

        def __init__(self, width_mult: float, depth: int = 1) -> None:
            super().__init__()
            self.enc = _Encoder(width_mult, depth)
            d = self.enc.embed_dim
            self.head = nn.Sequential(
                nn.Linear(d * 2, d), nn.ReLU(inplace=True), nn.Linear(d, 1))

        def forward(self, a: "torch.Tensor", b: "torch.Tensor") -> "torch.Tensor":
            ea, eb = self.enc(a), self.enc(b)
            pair = torch.cat([(ea - eb).abs(), ea * eb], dim=1)
            return self.head(pair).squeeze(1)

class DupCNN:
    """!
    @brief Feedback-trained Siamese CNN wrapper with a size knob and safe fallbacks.

    Every method is a no-op returning a neutral value when torch is missing, so
    callers can use this unconditionally and let the logistic model take over.
    """

    def __init__(self, width_mult: float = 1.0, depth: int = 1, size: str = "") -> None:
        self.width_mult: float = min(WIDTH_MAX, max(WIDTH_MIN, float(width_mult)))
        self.depth: int = max(1, int(depth))
        self.size: str = size
        self.trained: bool = False
        self.net = _SiameseNet(self.width_mult, self.depth) if _HAVE_TORCH else None

    @classmethod
    def sized(cls, size: str, sizes: "dict | None" = None) -> "DupCNN":
        """! @brief A fresh, untrained model of a named size from `sizes` (default table)."""
        sp = size_spec(size, sizes)
        return cls(sp["width"], sp["depth"], size=str(size).lower())

    @property
    def available(self) -> bool:
        """! @brief True when torch is importable and a model has been built."""
        return _HAVE_TORCH and self.net is not None

    @property
    def params(self) -> int:
        """! @brief Trainable parameter count (0 without torch)."""
        return sum(p.numel() for p in self.net.parameters()) if self.available else 0

    @classmethod
    def load(cls, path: str, width_mult: float = 1.0, depth: int = 1) -> "DupCNN":
        """!
        @brief Load a checkpoint if torch is present and the file exists. The
               checkpoint carries its own width/depth/size, so the arguments only
               shape the untrained fallback.
        @return A DupCNN; untrained (fallback) when torch is missing or load fails.
        """
        m = cls(width_mult, depth)
        if not _HAVE_TORCH:
            return m
        try:
            ckpt = torch.load(path, map_location="cpu")
            m.width_mult = float(ckpt.get("width_mult", width_mult))
            m.depth = int(ckpt.get("depth", 1))
            m.size = str(ckpt.get("size", ""))
            m.net = _SiameseNet(m.width_mult, m.depth)
            m.net.load_state_dict(ckpt["state_dict"])
            m.net.eval()
            m.trained = True
        except Exception:
            pass
        return m

    def save(self, path: str) -> bool:
        """! @brief Write the checkpoint as a torch .pt file; return success."""
        if not self.available:
            return False
        try:
            tmp = path + ".tmp"
            torch.save({"state_dict": self.net.state_dict(), "width_mult": self.width_mult,
                        "depth": self.depth, "size": self.size}, tmp)
            os.replace(tmp, path)
            return True
        except Exception:
            return False

    def bench(self, device: str = "cpu", batch: int = 256, reps: int = 5) -> "dict":
        """!
        @brief Speed vs parameters: params, inference ms per pair at batch 1 (the
               Pi / CPU case) and at `batch` (the GPU case), and peak memory of one
               training step at `batch` on a CUDA device.
        """
        if not self.available:
            return {}
        import time
        dev = device if (device != "cuda" or torch.cuda.is_available()) else "cpu"
        net = self.net.to(dev).eval()
        out = {"params": self.params, "device": dev, "batch": batch}
        with torch.no_grad():
            for n, key in ((1, "ms_per_pair_b1"), (batch, "ms_per_pair_batch")):
                a = torch.rand(n, 3, WORK, WORK, device=dev); b = torch.rand_like(a)
                net(a, b)
                if dev == "cuda":
                    torch.cuda.synchronize()
                t = time.perf_counter()
                for _ in range(reps):
                    net(a, b)
                if dev == "cuda":
                    torch.cuda.synchronize()
                out[key] = round((time.perf_counter() - t) / reps / n * 1000, 3)
        if dev == "cuda":
            torch.cuda.reset_peak_memory_stats()
            net.train()
            a = torch.rand(batch, 3, WORK, WORK, device=dev); b = torch.rand_like(a)
            y = torch.rand(batch, device=dev)
            nn.BCEWithLogitsLoss()(net(a, b), y).backward()
            net.zero_grad(set_to_none=True)
            torch.cuda.synchronize()
            out["train_mem_mb"] = round(torch.cuda.max_memory_allocated() / 2**20)
            net.eval()
        return out

    def predict(self, img_a: "np.ndarray", img_b: "np.ndarray") -> "float | None":
        """!
        @brief Probability the pair is a true duplicate.
        @return 0..1 probability, or None to signal the caller to fall back.
        """
        if not (self.available and self.trained):
            return None
        a = _to_work_bgr(img_a)
        b = _to_work_bgr(img_b)
        if a is None or b is None:
            return None
        try:
            with torch.no_grad():
                ta = torch.from_numpy(a[None])
                tb = torch.from_numpy(b[None])
                logit = self.net(ta, tb)
                return float(torch.sigmoid(logit)[0])
        except Exception:
            return None

    def fit_batches(self, batches, lr: float = 1e-3, device: str = "cpu",
                    _opt_holder: dict = None, amp: bool = False) -> "float | None":
        """!
        @brief One pass of minibatch training over an iterable of (a, b, y)
               numpy batches (a, b: [n,3,WORK,WORK] float32 as encode_pair stores
               them; y: [n] in {0,1}). For datasets that don't fit in memory
               (dedup_train streams millions of synthetic pairs through this).
        @param _opt_holder dict kept by the caller across calls so the optimizer
               state (Adam moments) survives between passes.
        @return Mean loss of the pass, or None when torch is missing.
        """
        if not self.available:
            return None
        self.net.to(device).train()
        holder = _opt_holder if _opt_holder is not None else {}
        opt = holder.get("opt")
        if opt is None or holder.get("lr") != lr:
            opt = torch.optim.Adam(self.net.parameters(), lr=lr)
            holder["opt"], holder["lr"] = opt, lr
        loss_fn = nn.BCEWithLogitsLoss()
        total, n = 0.0, 0
        # bf16 autocast on a GPU (CUDA or ROCm): ~2x throughput for the same
        # result on a net this small; no grad scaler needed with bf16.
        use_amp = bool(amp) and device != "cpu"
        for a, b, y in batches:
            ta = torch.from_numpy(np.ascontiguousarray(a)).to(device, non_blocking=True)
            tb = torch.from_numpy(np.ascontiguousarray(b)).to(device, non_blocking=True)
            ty = torch.as_tensor(np.asarray(y, np.float32)).to(device, non_blocking=True)
            opt.zero_grad(set_to_none=True)
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=use_amp):
                logit = self.net(ta, tb)
            loss = loss_fn(logit.float(), ty)
            loss.backward()
            opt.step()
            total += float(loss.item()) * len(ty); n += len(ty)
        self.net.eval()
        self.trained = self.trained or n > 0
        return total / n if n else None

    def predict_batch(self, a, b, device: str = "cpu"):
        """! @brief Probabilities for prepared [n,3,WORK,WORK] float32 batches."""
        if not self.available:
            return None
        self.net.to(device).eval()
        with torch.no_grad():
            z = self.net(torch.from_numpy(np.ascontiguousarray(a)).to(device),
                         torch.from_numpy(np.ascontiguousarray(b)).to(device))
            return torch.sigmoid(z).cpu().numpy()

    def fit(self, samples: "list[tuple[bytes, int]]", epochs: int = 30,
            lr: float = 1e-3, min_samples: int = 32) -> bool:
        """!
        @brief Train from encoded (npz-bytes, label) pairs.
        @param samples List of (encode_pair output, label in {0,1}).
        @return True if trained and ready; False when torch is missing, samples
                are too few, or only one class is present.
        """
        if not self.available or len(samples) < min_samples:
            return False
        a_list, b_list, y_list = [], [], []
        for blob, label in samples:
            try:
                d = np.load(io.BytesIO(blob))
                a_list.append(d["a"])
                b_list.append(d["b"])
                y_list.append(float(label))
            except Exception:
                continue
        if len(set(y_list)) < 2:
            return False
        a = torch.from_numpy(np.stack(a_list))
        b = torch.from_numpy(np.stack(b_list))
        y = torch.tensor(y_list, dtype=torch.float32)
        opt = torch.optim.Adam(self.net.parameters(), lr=lr)
        loss_fn = nn.BCEWithLogitsLoss()
        self.net.train()
        for _ in range(epochs):
            opt.zero_grad()
            logits = self.net(a, b)
            loss = loss_fn(logits, y)
            loss.backward()
            opt.step()
        self.net.eval()
        self.trained = True
        return True