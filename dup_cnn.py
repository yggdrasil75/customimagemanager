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

try:
    import torch
    from torch import nn
    _HAVE_TORCH = True
except Exception:
    _HAVE_TORCH = False

WORK: int = 128
WIDTH_MIN: float = 0.25
WIDTH_MAX: float = 2.0


def _to_work_bgr(img: "np.ndarray | None") -> "np.ndarray | None":
    """!
    @brief Resize any BGR/gray array to a WORKxWORKx3 float32 tensor in 0..1.
    @return CHW float32 array, or None if the image is unusable.
    """
    if img is None:
        return None
    try:
        import cv2
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

        def __init__(self, width_mult: float) -> None:
            super().__init__()
            c1, c2, c3, c4 = _channels(width_mult)
            self.net = nn.Sequential(
                nn.Conv2d(3, c1, 3, 2, 1), nn.BatchNorm2d(c1), nn.ReLU(inplace=True),
                nn.Conv2d(c1, c2, 3, 2, 1), nn.BatchNorm2d(c2), nn.ReLU(inplace=True),
                nn.Conv2d(c2, c3, 3, 2, 1), nn.BatchNorm2d(c3), nn.ReLU(inplace=True),
                nn.Conv2d(c3, c4, 3, 2, 1), nn.BatchNorm2d(c4), nn.ReLU(inplace=True),
                nn.AdaptiveAvgPool2d(1), nn.Flatten())
            self.embed_dim = c4

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            return self.net(x)

    class _SiameseNet(nn.Module):
        """! @brief Encode both images with a shared tower, classify the pair from |a-b| and a*b."""

        def __init__(self, width_mult: float) -> None:
            super().__init__()
            self.enc = _Encoder(width_mult)
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

    def __init__(self, width_mult: float = 1.0) -> None:
        self.width_mult: float = min(WIDTH_MAX, max(WIDTH_MIN, float(width_mult)))
        self.trained: bool = False
        self.net = _SiameseNet(self.width_mult) if _HAVE_TORCH else None

    @property
    def available(self) -> bool:
        """! @brief True when torch is importable and a model has been built."""
        return _HAVE_TORCH and self.net is not None

    @classmethod
    def load(cls, path: str, width_mult: float = 1.0) -> "DupCNN":
        """!
        @brief Load a checkpoint if torch is present and the file exists.
        @return A DupCNN; untrained (fallback) when torch is missing or load fails.
        """
        m = cls(width_mult)
        if not _HAVE_TORCH:
            return m
        try:
            ckpt = torch.load(path, map_location="cpu")
            m.width_mult = float(ckpt.get("width_mult", width_mult))
            m.net = _SiameseNet(m.width_mult)
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
            torch.save({"state_dict": self.net.state_dict(),
                        "width_mult": self.width_mult}, tmp)
            import os
            os.replace(tmp, path)
            return True
        except Exception:
            return False

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