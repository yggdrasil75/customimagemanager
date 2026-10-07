"""!
@brief HEARDU - learned duplicate scorer for audio (FLAC vs MP3 of one song).

A track is its log-mel spectrogram (16 kHz mono, 64 mels, 10 ms hop) cut
into 1 s windows every 0.5 s; each window is mean-normalised (gain-free)
and embedded by a small 2D CNN, the temporal block mixes neighbours, and two
tracks compare through the learned step-similarity matrix + DTW
(modules/dedup/seq_models). Trained to ignore codec, bitrate, sample rate,
loudness and EQ (the audio dataset's augmentations); a cut or excerpt
scores its shared fraction, a different song / mix ~0.
"""
import numpy as np

from modules.dedup import media_sig
from modules.dedup.seq_models import SeqDupModel, nn, conv_block

SR = 16000
N_FFT, HOP, N_MELS = 512, 160, 64
WIN, STEP = 100, 50                 # mel frames per window (1 s), per step (0.5 s)
MAX_S = 1200

_FB = {}


def mel_filterbank(sr: int = SR, n_fft: int = N_FFT, n_mels: int = N_MELS, fmin=50.0, fmax=8000.0):
    key = (sr, n_fft, n_mels)
    if key not in _FB:
        mel = lambda f: 2595.0 * np.log10(1.0 + f / 700.0)
        hz = lambda m: 700.0 * (10 ** (m / 2595.0) - 1.0)
        pts = hz(np.linspace(mel(fmin), mel(min(fmax, sr / 2)), n_mels + 2))
        bins = np.fft.rfftfreq(n_fft, 1.0 / sr)
        fb = np.zeros((n_mels, len(bins)), np.float32)
        for m in range(n_mels):
            lo, c, hi = pts[m], pts[m + 1], pts[m + 2]
            fb[m] = np.clip(np.minimum((bins - lo) / (c - lo), (hi - bins) / (hi - c)), 0, None)
        _FB[key] = fb
    return _FB[key]


def log_mel(pcm: np.ndarray) -> np.ndarray:
    """! @brief [frames, N_MELS] log-mel (dB) of mono float PCM at SR."""
    x = np.asarray(pcm, np.float32)
    if len(x) < N_FFT:
        return np.zeros((0, N_MELS), np.float32)
    n = 1 + (len(x) - N_FFT) // HOP
    win = np.hanning(N_FFT).astype(np.float32)
    fb = mel_filterbank()
    out = np.empty((n, N_MELS), np.float32)
    for s in range(0, n, 2048):
        idx = np.arange(s, min(n, s + 2048))[:, None] * HOP + np.arange(N_FFT)[None, :]
        p = np.abs(np.fft.rfft(x[idx] * win, axis=1)) ** 2
        out[s:s + len(p)] = 10 * np.log10(p @ fb.T + 1e-10)
    return out


def windows(mel: np.ndarray) -> np.ndarray:
    """! @brief [frames, N_MELS] -> steps [T, 1, N_MELS, WIN], each mean-normalised."""
    if len(mel) < WIN:
        if not len(mel):
            return np.zeros((0, 1, N_MELS, WIN), np.float32)
        mel = np.pad(mel, ((0, WIN - len(mel)), (0, 0)), mode="edge")
    starts = range(0, len(mel) - WIN + 1, STEP)
    w = np.stack([mel[s:s + WIN].T for s in starts])[:, None]
    w = w - w.mean(axis=(2, 3), keepdims=True)
    return (w / 20.0).astype(np.float32)


def steps_from_pcm(pcm: np.ndarray) -> np.ndarray:
    return windows(log_mel(pcm))


class HEARDU(SeqDupModel):
    FAMILY = "HEARDU"
    ARCH = "heardu"
    STEP_SHAPE = (1, N_MELS, WIN)

    def _step_encoder(self, C: int):
        c1 = max(8, C // 2)
        return nn.Sequential(*conv_block(1, c1), *conv_block(c1, C), *conv_block(C, 2 * C),
                             *conv_block(2 * C, 2 * C), nn.AdaptiveAvgPool2d(1), nn.Flatten(),
                             nn.Linear(2 * C, C))

    @staticmethod
    def steps_from_path(path: str) -> "np.ndarray | None":
        pcm = media_sig.decode_audio(path, SR, MAX_S)
        return None if pcm is None else steps_from_pcm(pcm)
