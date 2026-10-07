"""! @file
@brief HEURDUV - learned video duplicate scorer (dedup_cnn_video).
======================================================================
Scores pairs of kind "video" (more than 30 source frames; animations up to
30 frames stay with HEURDU in dedup_cnn) with the HEURDUV sequence model
(heurduv.py). Offered as the "dedup.video" capability in Settings > Models;
sizes come from huggingface.co/yggdrasil75/HEURDUV on first use, a size
trained in Trainer > Dedup (models/heurduv_<size>.pt) overrides it.

Priority 30, above dedup_cnn (20): when HEURDUV has a checkpoint it answers
video pairs; until then it reports unavailable and the legacy 3D clip model
(dedup_cnn, dup_cnn_video.pt) or the naive video score answer, exactly as
before. Merge / "not a duplicate" decisions on videos are stored in
heurduv_samples for the trainer.
"""
import os

from modules.dedup import media_sig, seq_align
from modules.dedup.seq_module import register_seq_module, path_to_map
from . import heurduv

MANIFEST = {
    "id":          "dedup_cnn_video",
    "name":        "Video duplicates (HEURDUV)",
    "version":     "1.0.0",
    "description": "Learned duplicate scorer for videos (> 30 frames): trims, re-encodes, "
                   "fps changes, rescales. Needs torch.",
    "core":        False,
    "requires":    ["dedup"],
    "pip":         ["torch"],
    "assets":      [],
}

HF_REPO = "yggdrasil75/HEURDUV"
HF_SIZES = ["nano", "small", "medium", "large"]


def _map(sa, sb, abs_a, abs_b):
    """! @brief Step map for a merged pair: DTW path over the frames' 1024-bit hashes."""
    ha, hb = media_sig.frame_hashes(sa)[1], media_sig.frame_hashes(sb)[1]
    return path_to_map(seq_align.dtw_path(media_sig.seq_phash_cost(ha, hb)), len(sb))


def register(host):
    svc = register_seq_module(
        host, cls=heurduv.HEURDUV, kind="video", cap="dedup.video", cap_label="Video duplicate scorer",
        cap_summary="Fraction of two videos' timelines that is the same content (0..1).",
        cap_input="two video paths", prefix="heurduv", hf_repo=HF_REPO, hf_sizes=HF_SIZES,
        sample_table="heurduv_samples", module_dir=os.path.dirname(os.path.abspath(__file__)),
        label="HEURDUV", map_fn=_map, priority=30,
        note="Sequence CNN over 1 fps frames + DTW. nano/small for a Pi, medium for most. Sizes download "
             "from huggingface.co/" + HF_REPO + " on first use; until one exists the legacy clip "
             "model / naive video score answer.")
    if svc:
        host.provide_service("dedup_cnn_video", svc)
