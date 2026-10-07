"""!
@brief HEURDUV - learned duplicate scorer for video (> 30 frames).

A clip is its timeline at media_sig.VIDEO_FPS (lowered for clips longer
than media_sig.MAX_STEPS steps), each frame SIDE x SIDE RGB. A small 2D CNN
embeds every frame, the temporal block (residual Conv1d over time) mixes
neighbours so motion and order count, and two clips compare through the
learned step-similarity matrix + DTW (modules/dedup/seq_models). Robust to
re-encode, rescale, fps change, letterbox, light crops and colour shifts
(the video dataset's augmentations); a trim scores its shared fraction.
"""
import numpy as np

from modules.dedup import media_sig
from modules.dedup.seq_models import SeqDupModel, _HAVE_TORCH, nn, conv_block

SIDE = 112


class HEURDUV(SeqDupModel):
    FAMILY = "HEURDUV"
    ARCH = "heurduv"
    STEP_SHAPE = (3, SIDE, SIDE)

    def _step_encoder(self, C: int):
        c1, c2 = max(8, C // 2), C
        return nn.Sequential(*conv_block(3, c1), *conv_block(c1, c2), *conv_block(c2, 2 * c2),
                             *conv_block(2 * c2, 2 * c2), nn.AdaptiveAvgPool2d(1), nn.Flatten(),
                             nn.Linear(2 * c2, C))

    @staticmethod
    def prep(x: np.ndarray) -> np.ndarray:
        x = np.asarray(x)
        if x.dtype == np.uint8:                        # [T, H, W, 3] RGB frames
            return x.transpose(0, 3, 1, 2).astype(np.float32) / 255.0
        return x.astype(np.float32)

    @staticmethod
    def frames_from_path(path: str) -> "np.ndarray | None":
        """! @brief uint8 [T, SIDE, SIDE, 3] timeline (the stored / training form)."""
        r = media_sig.decode_frames(path, square=SIDE)
        return None if r is None else r[0]

    @staticmethod
    def steps_from_path(path: str) -> "np.ndarray | None":
        return HEURDUV.frames_from_path(path)
