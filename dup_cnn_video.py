"""!
@brief Siamese 3D-CNN duplicate classifier for *videos*, trained from user feedback.

WHY A SEPARATE MODEL
--------------------
The still-image models (dup_heuristics.DuplicateClassifier, dup_cnn.DupCNN)
compare two frames. Extracting a few frames from each of two videos and
comparing them pair-by-pair is NOT temporal awareness: the network never sees
motion, cannot tell a clip from its own frames shuffled, and any cross-frame
reasoning is a hand-written rule, not something learned.

This model ingests a clip as a spatiotemporal volume [C, T, H, W] and uses
nn.Conv3d, so the convolutions span time as well as space. The network learns
temporal features (motion, ordering, pacing) itself. Two clips are each encoded
by a shared 3D tower into one embedding; a small head classifies the pair from
|a-b| and a*b, exactly like the image Siamese. Robustness to a dropped/inserted
frame is a LEARNED property, reinforced by temporal augmentation at train time
(random frame drops), not a fixed alignment heuristic.

PLUMBING
--------
Mirrors dup_cnn.DupCNN so manager.py can treat it the same way:
  * lazy construction after config (width_mult knob),
  * load()/save() a .pt checkpoint,
  * fit() from stored (npz-bytes, label) samples,
  * every method is a safe no-op returning a neutral value when torch is
    missing, so callers fall back to the frame/logistic path.

Samples are whole clip tensors, so this has its OWN store
(dup_cnn_video_samples) and cannot share the image CNN's frame-pair table.
"""

import io
import numpy as np

try:
    import torch
    from torch import nn
    import torch.nn.functional as F
    _HAVE_TORCH = True
except Exception:
    _HAVE_TORCH = False

# A clip is normalized to T frames of WORK x WORK. Kept small: this runs on CPU
# for most installs and is only a similarity signal, not a generation model.
WORK: int = 112
CLIP_T: int = 16          # frames per clip fed to the network
WIDTH_MIN: float = 0.25
WIDTH_MAX: float = 2.0


def _to_work_frame(img: "np.ndarray | None") -> "np.ndarray | None":
    """Resize any BGR/gray frame to HxWx3 float32 in 0..1 (H=W=WORK)."""
    if img is None:
        return None
    try:
        import cv2
        if img.ndim == 2:
            img = np.repeat(img[:, :, None], 3, axis=2)
        bgr = img[:, :, :3]
        r = cv2.resize(bgr, (WORK, WORK), interpolation=cv2.INTER_AREA)
        return r.astype(np.float32) / 255.0
    except Exception:
        return None


def clip_to_volume(frames: "list[np.ndarray]") -> "np.ndarray | None":
    """!
    @brief Turn a variable-length list of frames into a fixed [C, T, H, W] volume.

    Resamples to exactly CLIP_T frames by even index selection (so clips of any
    length/fps map onto the same temporal grid), normalizes each frame, and lays
    them out channel-first with time as the depth axis for Conv3d.

    @return float32 array shaped [3, CLIP_T, WORK, WORK], or None if unusable.
    """
    if not frames:
        return None
    n = len(frames)
    # Evenly sample CLIP_T source indices across the clip (with replacement when
    # the clip is shorter than CLIP_T, so short clips still fill the volume).
    idx = np.linspace(0, n - 1, CLIP_T).round().astype(int)
    vol = []
    for k in idx:
        w = _to_work_frame(frames[int(k)])
        if w is None:
            return None
        vol.append(w)                       # each HxWx3
    arr = np.stack(vol, axis=0)             # [T, H, W, C]
    arr = arr.transpose(3, 0, 1, 2)         # [C, T, H, W]
    return np.ascontiguousarray(arr, dtype=np.float32)


def encode_pair(frames_a: "list[np.ndarray]",
                frames_b: "list[np.ndarray]") -> "bytes | None":
    """!
    @brief Serialize a clip pair to a stored training sample (two [C,T,H,W] volumes).
    @return npz bytes of arrays 'a' and 'b', or None if either clip is unusable.
    """
    a = clip_to_volume(frames_a)
    b = clip_to_volume(frames_b)
    if a is None or b is None:
        return None
    buf = io.BytesIO()
    np.savez_compressed(buf, a=a, b=b)
    return buf.getvalue()


def _channels(width_mult: float) -> "list[int]":
    base = [16, 32, 64, 128]
    return [max(4, int(round(c * width_mult))) for c in base]


if _HAVE_TORCH:
    class _Encoder3D(nn.Module):
        """! @brief Shared 3D-conv tower mapping one clip [C,T,H,W] to an embedding.

        Strides reduce space quickly but time more gently, so temporal structure
        survives several layers before the final global pool over (T,H,W)."""

        def __init__(self, width_mult: float) -> None:
            super().__init__()
            c1, c2, c3, c4 = _channels(width_mult)
            # (kernel), (stride) as (T, H, W). Space downsamples every layer;
            # time downsamples only in layers 2 and 4, preserving motion cues.
            self.net = nn.Sequential(
                nn.Conv3d(3,  c1, (3, 3, 3), (1, 2, 2), (1, 1, 1)),
                nn.BatchNorm3d(c1), nn.ReLU(inplace=True),
                nn.Conv3d(c1, c2, (3, 3, 3), (2, 2, 2), (1, 1, 1)),
                nn.BatchNorm3d(c2), nn.ReLU(inplace=True),
                nn.Conv3d(c2, c3, (3, 3, 3), (1, 2, 2), (1, 1, 1)),
                nn.BatchNorm3d(c3), nn.ReLU(inplace=True),
                nn.Conv3d(c3, c4, (3, 3, 3), (2, 2, 2), (1, 1, 1)),
                nn.BatchNorm3d(c4), nn.ReLU(inplace=True),
                nn.AdaptiveAvgPool3d(1), nn.Flatten())
            self.embed_dim = c4

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            return self.net(x)

    class _SiameseVideoNet(nn.Module):
        """! @brief Encode both clips with a shared 3D tower; classify from |a-b| and a*b."""

        def __init__(self, width_mult: float) -> None:
            super().__init__()
            self.enc = _Encoder3D(width_mult)
            d = self.enc.embed_dim
            self.head = nn.Sequential(
                nn.Linear(d * 2, d), nn.ReLU(inplace=True), nn.Linear(d, 1))

        def forward(self, a: "torch.Tensor", b: "torch.Tensor") -> "torch.Tensor":
            ea, eb = self.enc(a), self.enc(b)
            pair = torch.cat([(ea - eb).abs(), ea * eb], dim=1)
            return self.head(pair).squeeze(1)


def _augment_drop(vol: "np.ndarray", max_drop: int = 2) -> "np.ndarray":
    """Randomly drop up to max_drop frames from a [C,T,H,W] volume and repeat the
    previous frame to keep length T. Teaches the model that a clip with a missing
    frame is still the same clip — the frame-drop tolerance you actually want,
    learned rather than hand-coded."""
    import random
    T = vol.shape[1]
    k = random.randint(0, max_drop)
    if k == 0:
        return vol
    keep = sorted(random.sample(range(T), max(1, T - k)))
    out = vol[:, keep, :, :]
    # Pad back to T by repeating the last kept frame.
    pad = T - out.shape[1]
    if pad > 0:
        tail = np.repeat(out[:, -1:, :, :], pad, axis=1)
        out = np.concatenate([out, tail], axis=1)
    return np.ascontiguousarray(out)


class DupVideoCNN:
    """!
    @brief Feedback-trained Siamese 3D-CNN for video duplicates, with safe fallbacks.

    Every method is a no-op returning a neutral value when torch is missing, so
    manager.py can call it unconditionally and let the frame/logistic path take
    over when this model is unavailable or untrained.
    """

    def __init__(self, width_mult: float = 1.0) -> None:
        self.width_mult: float = min(WIDTH_MAX, max(WIDTH_MIN, float(width_mult)))
        self.trained: bool = False
        self.net = _SiameseVideoNet(self.width_mult) if _HAVE_TORCH else None

    @property
    def available(self) -> bool:
        """! @brief True when torch is importable and a model has been built."""
        return _HAVE_TORCH and self.net is not None

    @classmethod
    def load(cls, path: str, width_mult: float = 1.0) -> "DupVideoCNN":
        """! @brief Load a checkpoint; untrained (fallback) if torch missing or load fails."""
        m = cls(width_mult)
        if not _HAVE_TORCH:
            return m
        try:
            ckpt = torch.load(path, map_location="cpu")
            m.width_mult = float(ckpt.get("width_mult", width_mult))
            m.net = _SiameseVideoNet(m.width_mult)
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
            import os
            tmp = path + ".tmp"
            torch.save({"state_dict": self.net.state_dict(),
                        "width_mult": self.width_mult}, tmp)
            os.replace(tmp, path)
            return True
        except Exception:
            return False

    def predict(self, frames_a: "list[np.ndarray]",
                frames_b: "list[np.ndarray]") -> "float | None":
        """!
        @brief Probability two clips are the same video.
        @param frames_a/b Decoded RGB/BGR frames (any count); resampled to CLIP_T.
        @return 0..1 probability, or None to signal the caller to fall back.
        """
        if not (self.available and self.trained):
            return None
        a = clip_to_volume(frames_a)
        b = clip_to_volume(frames_b)
        if a is None or b is None:
            return None
        try:
            with torch.no_grad():
                ta = torch.from_numpy(a[None])   # [1, C, T, H, W]
                tb = torch.from_numpy(b[None])
                logit = self.net(ta, tb)
                return float(torch.sigmoid(logit)[0])
        except Exception:
            return None

    def fit(self, samples: "list[tuple[bytes, int]]", epochs: int = 25,
            lr: float = 1e-3, min_samples: int = 24, batch: int = 8,
            augment: bool = True) -> bool:
        """!
        @brief Train from encoded (npz-bytes, label) clip pairs.
        @param samples List of (encode_pair output, label in {0,1}).
        @param augment Apply random frame-drop augmentation to positive pairs so
               drop tolerance is learned.
        @return True if trained and ready; False when torch is missing, samples
                are too few, or only one class is present.
        """
        if not self.available or len(samples) < min_samples:
            return False
        a_list, b_list, y_list = [], [], []
        for blob, label in samples:
            try:
                d = np.load(io.BytesIO(blob))
                a_list.append(d["a"]); b_list.append(d["b"]); y_list.append(float(label))
            except Exception:
                continue
        if len(set(y_list)) < 2:
            return False

        opt = torch.optim.Adam(self.net.parameters(), lr=lr)
        loss_fn = nn.BCEWithLogitsLoss()
        n = len(y_list)
        self.net.train()
        try:
            for _ in range(epochs):
                order = np.random.permutation(n)
                for s in range(0, n, batch):
                    sel = order[s:s + batch]
                    ba, bb, by = [], [], []
                    for j in sel:
                        va, vb = a_list[j], b_list[j]
                        if augment and y_list[j] > 0.5:
                            va = _augment_drop(va); vb = _augment_drop(vb)
                        ba.append(va); bb.append(vb); by.append(y_list[j])
                    ta = torch.from_numpy(np.stack(ba))
                    tb = torch.from_numpy(np.stack(bb))
                    ty = torch.tensor(by, dtype=torch.float32)
                    opt.zero_grad()
                    loss = loss_fn(self.net(ta, tb), ty)
                    loss.backward()
                    opt.step()
        except Exception:
            return False
        self.net.eval()
        self.trained = True
        return True