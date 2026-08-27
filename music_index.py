"""
music_index.py — music side of the media manager.
=================================================

Self-contained module mirroring the conventions of image_index.py /
object_grouping.py so it bolts onto manager.py without touching the image
pipeline.

What it provides
----------------
- A `music` table (one row per track) holding the metadata the UI browses and
  edits, plus an `emb` BLOB (float32 audio embedding) and a `cluster` id.
- `MUSIC_EXTS`: the formats we index. Unlike images we do NOT convert — there
  is no lossless shrink for audio — we only organise + tag in place.
- Resumable indexing: a restart skips any track already in the DB whose mtime
  matches, so a 50k-track scan resumes instead of restarting.
- Metadata read/write across mp3/flac/m4a/ogg/opus/wav via mutagen, normalised
  to one dict shape regardless of container.
- A deterministic, offline audio embedding (librosa MFCC + chroma + spectral
  contrast + tempo) — no model download, no GPU. Good enough to cluster by
  "sounds like" and to power shuffle-by-similarity.
- KMeans clustering over the embeddings.
- "Shuffle by X": given a seed track or artist, order the library by cosine
  similarity to the seed centroid with a little randomness so it's a playlist,
  not a deterministic sort.

Everything is lazy: librosa is only imported when an embedding is actually
computed, so indexing/metadata work even on a box without the audio stack.
"""
from __future__ import annotations
import os, json, time, struct, threading
import numpy as np

# Containers we understand. Kept lowercase, with dot.
MUSIC_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".aac",
              ".ogg", ".oga", ".opus", ".wav", ".wma", ".aiff", ".aif"}

EMB_DIM = 62          # MFCC(20*2) + chroma(12) + contrast(7) + tempo(1) + zcr(1) + rms(1)
EMB_SIG = "librosa-v1"  # bump to invalidate every cached embedding

_lock = threading.Lock()

# ── schema ────────────────────────────────────────────────────────────────────
def ensure_tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS music (
            rel_path     TEXT PRIMARY KEY,
            mtime        REAL,
            size         INTEGER,
            duration     REAL,           -- seconds
            bitrate      INTEGER,
            samplerate   INTEGER,
            channels     INTEGER,
            -- editable metadata --
            title        TEXT DEFAULT '',
            artist       TEXT DEFAULT '',
            album        TEXT DEFAULT '',
            albumartist  TEXT DEFAULT '',
            track        INTEGER,
            disc         INTEGER,
            year         TEXT DEFAULT '',
            genre        TEXT DEFAULT '',
            composer     TEXT DEFAULT '',
            comment      TEXT DEFAULT '',
            tags         TEXT DEFAULT '[]',   -- free-form user tags (JSON list)
            -- derived --
            emb          BLOB,
            emb_sig      TEXT,
            cluster      INTEGER DEFAULT -1,
            created      REAL
        );
        CREATE INDEX IF NOT EXISTS idx_music_artist  ON music(artist);
        CREATE INDEX IF NOT EXISTS idx_music_album   ON music(album);
        CREATE INDEX IF NOT EXISTS idx_music_cluster ON music(cluster);

        -- cached cluster labels (k chosen at run time)
        CREATE TABLE IF NOT EXISTS music_clusters (
            cluster   INTEGER PRIMARY KEY,
            label     TEXT DEFAULT '',
            size      INTEGER DEFAULT 0,
            created   REAL
        );
    """)
    db.commit()

# ── metadata (mutagen) ─────────────────────────────────────────────────────────
def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            if isinstance(v, (list, tuple)):
                v = v[0]
            return str(v)
    return ""

def _split_num(s):
    """'3/12' -> 3 ; '7' -> 7 ; '' -> None"""
    if s is None:
        return None
    s = str(s).split('/')[0].strip()
    try:
        return int(s)
    except (ValueError, TypeError):
        return None

def read_audio_metadata(abs_path: str) -> dict:
    """Normalise tags from any container into one flat dict. Never raises."""
    from mutagen import File as MutagenFile
    out = {
        "title": "", "artist": "", "album": "", "albumartist": "",
        "track": None, "disc": None, "year": "", "genre": "",
        "composer": "", "comment": "",
        "duration": 0.0, "bitrate": 0, "samplerate": 0, "channels": 0,
    }
    try:
        mf = MutagenFile(abs_path, easy=True)
    except Exception:
        mf = None
    if mf is None:
        return out

    info = getattr(mf, "info", None)
    if info is not None:
        out["duration"]   = float(getattr(info, "length", 0) or 0)
        out["bitrate"]    = int(getattr(info, "bitrate", 0) or 0)
        out["samplerate"] = int(getattr(info, "sample_rate", 0) or 0)
        out["channels"]   = int(getattr(info, "channels", 0) or 0)

    t = dict(mf.tags or {})
    out["title"]       = _first(t, "title")
    out["artist"]      = _first(t, "artist")
    out["album"]       = _first(t, "album")
    out["albumartist"] = _first(t, "albumartist", "album artist")
    out["genre"]       = _first(t, "genre")
    out["composer"]    = _first(t, "composer")
    out["comment"]     = _first(t, "comment")
    out["year"]        = _first(t, "date", "year", "originaldate")[:10]
    out["track"]       = _split_num(_first(t, "tracknumber", "track"))
    out["disc"]        = _split_num(_first(t, "discnumber", "disc"))
    return out

# mapping from our flat keys to EasyID3/easy-mp4/vorbis key names mutagen accepts
_EASY_KEYS = {
    "title": "title", "artist": "artist", "album": "album",
    "albumartist": "albumartist", "genre": "genre",
    "composer": "composer", "year": "date",
}

def write_audio_metadata(abs_path: str, meta: dict) -> bool:
    """Write editable fields back into the file. Returns True on success."""
    from mutagen import File as MutagenFile
    try:
        mf = MutagenFile(abs_path, easy=True)
        if mf is None:
            return False
        if mf.tags is None:
            mf.add_tags()
        for flat, ezk in _EASY_KEYS.items():
            if flat in meta and meta[flat] is not None:
                val = str(meta[flat])
                if val == "":
                    mf.tags.pop(ezk, None)
                else:
                    mf.tags[ezk] = val
        if meta.get("track") not in (None, ""):
            mf.tags["tracknumber"] = str(meta["track"])
        if meta.get("disc") not in (None, ""):
            mf.tags["discnumber"] = str(meta["disc"])
        if meta.get("comment") is not None:
            # comment isn't in every easy profile; best-effort
            try:
                mf.tags["comment"] = str(meta["comment"])
            except Exception:
                pass
        mf.save()
        return True
    except Exception:
        return False

# ── embedding ──────────────────────────────────────────────────────────────────
def _pack_emb(vec: np.ndarray) -> bytes:
    v = np.asarray(vec, dtype=np.float32).ravel()
    return struct.pack("<I", v.size) + v.tobytes()

def unpack_emb(blob) -> np.ndarray | None:
    if not blob:
        return None
    try:
        n = struct.unpack("<I", blob[:4])[0]
        return np.frombuffer(blob[4:4 + n * 4], dtype=np.float32).copy()
    except Exception:
        return None

def compute_embedding(abs_path: str, max_seconds: float = 90.0) -> np.ndarray | None:
    """Deterministic offline audio fingerprint suitable for similarity/clustering.

    Loads up to `max_seconds` (mono, 22.05 kHz), takes summary statistics of
    timbral + harmonic + rhythmic features, and concatenates them into a single
    fixed-length vector. No network, no model weights.
    """
    try:
        import librosa
    except Exception:
        return None
    try:
        y, sr = librosa.load(abs_path, sr=22050, mono=True, duration=max_seconds)
        if y is None or y.size < sr:          # < 1s of audio -> skip
            return None

        mfcc      = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=20)
        chroma    = librosa.feature.chroma_stft(y=y, sr=sr)
        contrast  = librosa.feature.spectral_contrast(y=y, sr=sr)
        zcr       = librosa.feature.zero_crossing_rate(y)
        rms       = librosa.feature.rms(y=y)
        tempo, _  = librosa.beat.beat_track(y=y, sr=sr)

        parts = [
            mfcc.mean(axis=1), mfcc.std(axis=1),   # 40
            chroma.mean(axis=1),                   # 12
            contrast.mean(axis=1),                 # 7
            np.array([float(np.atleast_1d(tempo)[0]) / 250.0]),  # 1 (normalised)
            np.array([float(zcr.mean())]),         # 1
            np.array([float(rms.mean())]),         # 1
        ]
        vec = np.concatenate(parts).astype(np.float32)
        # guard against NaN/inf from silent or corrupt files
        vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)
        return vec
    except Exception:
        return None

def normalize_matrix(M: np.ndarray) -> np.ndarray:
    """Z-score per column then L2-normalise rows -> cosine == dot product."""
    M = np.asarray(M, dtype=np.float32)
    mu = M.mean(axis=0, keepdims=True)
    sd = M.std(axis=0, keepdims=True) + 1e-6
    Z = (M - mu) / sd
    n = np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9
    return Z / n

# ── clustering ──────────────────────────────────────────────────────────────────
def cluster_embeddings(paths, embs, k=None):
    """KMeans over a list of embeddings. Returns {rel_path: cluster_id} and k."""
    from sklearn.cluster import KMeans
    X = normalize_matrix(np.vstack(embs))
    n = len(paths)
    if k is None:
        k = max(2, min(40, int(round(np.sqrt(n / 2)))))
    k = min(k, n)
    km = KMeans(n_clusters=k, n_init=4, random_state=0).fit(X)
    return {p: int(c) for p, c in zip(paths, km.labels_)}, k

# ── similarity / shuffle ────────────────────────────────────────────────────────
def shuffle_by(seed_vecs, all_paths, all_embs, temperature=0.25, limit=500):
    """Order tracks by similarity to the seed centroid, with controlled noise.

    seed_vecs : list of embeddings defining the seed (one song, or every song by
                an artist). Their mean is the centroid.
    Returns rel_paths ordered most->least similar, jittered so repeated presses
    give a fresh-but-coherent playlist.
    """
    if not all_embs:
        return []
    M = normalize_matrix(np.vstack(all_embs))
    centroid = normalize_matrix(np.vstack(seed_vecs)).mean(axis=0)
    centroid = centroid / (np.linalg.norm(centroid) + 1e-9)
    sims = M @ centroid                      # cosine, already row-normalised
    # add gaussian jitter scaled by temperature so the order is a playlist
    noise = np.random.normal(0, temperature, size=sims.shape)
    score = sims + noise
    order = np.argsort(-score)
    return [all_paths[i] for i in order[:limit]]