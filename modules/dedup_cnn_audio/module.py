"""! @file
@brief HEARDU - learned audio duplicate scorer (dedup_cnn_audio).
======================================================================
Scores pairs of kind "audio" (the music module's tracks) with the HEARDU
sequence model (heardu.py): a FLAC and the MP3 made from it, a 96k
re-rip, a quieter / EQ'd copy are the same song; a live version, a remix or
a different track are not. Offered as the "dedup.audio" capability in
Settings > Models; sizes come from huggingface.co/yggdrasil75/HEARDU on
first use, a size trained in Trainer > Dedup (models/heardu_<size>.pt)
overrides it. Until a checkpoint exists the scorer reports unavailable and
the naive audio score answers. Merge / "not a duplicate" decisions on
tracks are stored in heardu_samples for the trainer.
"""
import os

import numpy as np

from modules.dedup import media_sig
from modules.dedup.seq_module import register_seq_module
from . import heardu

MANIFEST = {
    "id":          "dedup_cnn_audio",
    "name":        "Audio duplicates (HEARDU)",
    "version":     "1.0.0",
    "description": "Learned duplicate scorer for audio: the same song across codecs, bitrates, "
                   "loudness and cuts. Needs torch.",
    "core":        False,
    "requires":    ["dedup"],
    "pip":         ["torch"],
    "assets":      [],
}

HF_REPO = "yggdrasil75/HEARDU"
HF_SIZES = ["nano", "small", "medium", "large"]


def _map(sa, sb, abs_a, abs_b):
    """! @brief Step map for a merged pair: the fingerprint offset, as a step shift."""
    ga, gb = media_sig.compute_audio_sig(abs_a), media_sig.compute_audio_sig(abs_b)
    off = media_sig.audio_offset(ga["fp"], gb["fp"]) if ga and gb else 0
    shift = int(round((off or 0) * media_sig.AUDIO_HOP / media_sig.AUDIO_SR / (heardu.STEP * heardu.HOP / heardu.SR)))
    mp = np.arange(len(sb)) - shift
    mp[(mp < 0) | (mp >= len(sa))] = -1
    return mp


def register(host):
    svc = register_seq_module(
        host, cls=heardu.HEARDU, kind="audio", cap="dedup.audio", cap_label="Audio duplicate scorer",
        cap_summary="Fraction of two tracks that is the same recording (0..1).",
        cap_input="two audio paths", prefix="heardu", hf_repo=HF_REPO, hf_sizes=HF_SIZES,
        sample_table="heardu_samples", module_dir=os.path.dirname(os.path.abspath(__file__)),
        label="HEARDU", map_fn=_map, priority=30,
        note="Sequence CNN over 1 s log-mel windows + DTW. Sizes download from huggingface.co/"
             + HF_REPO + " on first use; until one exists the naive audio score answers.")
    if svc:
        host.provide_service("dedup_cnn_audio", svc)
