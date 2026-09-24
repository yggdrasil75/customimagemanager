"""
Personal IQA — learns the user's own taste from their star ratings.
======================================================================
Tokens per image (see net.py):
  embed     whole image through the encoder            (encoder picked below)
  tile      3x3 full-resolution tiles through the same encoder (detail survives)
  face      aligned 106-pt facial geometry + head pose, one token per face,
            read from face_regions.shape (written by the face scan)
  pose17 / pose133   one token per person from the stored skeleton (follows
            the user's pose setting; 17-pt body or 133-pt whole-body),
            hip-centred, torso-normalised
  iqa       base no-reference IQA score (a different provider) on the full-res image
  tags      the file's tags, hashed

Source of truth stays where the app keeps it: tags in files/XMP, pose in the
sidecar, faces in face_regions — all read live at train and score time. The
only thing cached here is the encoder + base-IQA output, keyed on file mtime
and the encoder/base ids, because those are the expensive, image-only parts.

Training: fixed hash-based 15% validation split; retrain = new ratings + 4x
random replay of already-seen ones; val MSE + Spearman reported; the provider
is only 'available' once it beats the base IQA's Spearman on the val set.
Capacity tier follows the rating count; growing vs rebuilding is a setting.
"""
import json, os, random, threading, time, zlib

from flask import jsonify

from modules.model_broker import NoProviderError
from optional_deps import optional_import

torch, _HAVE_TORCH = optional_import("torch")
AVAILABLE = _HAVE_TORCH
UNAVAILABLE_REASON = "torch not installed"
if _HAVE_TORCH:
    from . import net
import numpy as np
import model_registry
import common

MANIFEST = {
    "id":          "personal_iqa",
    "name":        "Personal IQA (learned taste)",
    "version":     "1.0.0",
    "description": "Transformer scorer trained on YOUR star ratings from image/tile "
                   "embeddings, facial geometry, pose, base IQA and tags. Needs torch.",
    "core":        False,
    "requires":    ["rating"],
    "pip":         ["torch"],
    "assets":      ["personal_iqa.js"],
}

_DDL = """
CREATE TABLE IF NOT EXISTS personal_iqa_cache (
    rel_path TEXT PRIMARY KEY,
    mtime    REAL,
    key      TEXT,      -- encoder id + base iqa id + grid; mismatch = recompute
    embed    TEXT,      -- json [floats]
    tiles    TEXT,      -- json [[floats], ...]
    base_iqa REAL,
    trained  INTEGER DEFAULT 0   -- 1 once used in a training pass (replay pool)
);
"""
_KEY = "iqa:personal"
GRID, VAL_PCT, MIN_RATINGS = 3, 15, 50
# Token types a sample MUST have to be trained on. The point of the personal
# scorer is "idealised proportions + expression + a clear view", so an image
# with no pose or no face teaches nothing about that and is skipped rather
# than turning this into yet another blur detector. personal_iqa_required
# (comma list of embed, iqa, face, pose, tags) overrides.
REQUIRED_DEFAULT = "embed,iqa,face,pose"


def missing_parts(feats, required):
    """Which of the required token types a features dict lacks."""
    have = {"embed": bool(feats.get("embed")), "iqa": bool(feats.get("iqa")), "face": bool(feats.get("face")),
            "pose": bool(feats.get("pose17") or feats.get("pose133")),
            "tags": any(feats.get("tags") or [])}
    return [r for r in required if not have.get(r, True)]
REPLAY_RATIO, EPOCHS, BATCH, LR = 4, 5, 64, 1e-3


def _is_val(rel_path):
    return zlib.crc32(rel_path.encode()) % 100 < VAL_PCT


def _norm_pose(kps):
    """[{x,y,v}] -> hip-centred, torso-scaled flat [x,y,...]. Works for 17 and 133 (first 17 = COCO)."""
    P = np.array([[k["x"], k["y"]] for k in kps], np.float32)
    hip, sho = (P[11] + P[12]) / 2, (P[5] + P[6]) / 2
    s = float(np.linalg.norm(sho - hip)) or 1.0
    return ((P - hip) / s).ravel().tolist()


def register(host):
    core = host.core

    host.add_asset("personal_iqa.js")
    host.add_settings_tab("personal_iqa", "Personal IQA", icon="🎯")
    host.add_table(_DDL)
    host.add_config_key("personal_iqa_base", default="nima")
    host.add_config_key("personal_iqa_encoder", default="")       # "" = the selected embed provider
    host.add_config_key("personal_iqa_grow", default=False)       # tier change: grow (Net2Net) vs rebuild
    host.add_config_key("personal_iqa_required", default=REQUIRED_DEFAULT, validate=lambda v: str(v or ""))
    host.add_settings_field(key="personal_iqa_required", label="Required data per training image",
                            kind="text", pane="module",
                            help="Comma list of embed, iqa, face, pose, tags. Images missing any of these are "
                                 "left out of training (personal Retrain and Trainer > IQA alike).")
    # personal_iqa_cache.extra: faces + pose for images OUTSIDE the library
    # (iqa_train datasets), which have no face_regions rows or sidecar.
    host.on_startup(lambda: _migrate())

    def _migrate():
        try:
            host.db().execute("ALTER TABLE personal_iqa_cache ADD COLUMN extra TEXT")
            host.db().commit()
        except Exception:
            pass

    def required():
        return [r.strip() for r in str(host.config.get("personal_iqa_required") or "").split(",") if r.strip()]
    host.add_config_key("personal_iqa_sizes", default=net.sizes_text() if _HAVE_TORCH else "",
                        validate=lambda v: str(v or ""))
    host.add_settings_field(key="personal_iqa_sizes", label="Personal IQA size table", kind="textarea",
                            pane="module", help="One size per line: name d depth (d = transformer width, "
                                                "multiple of 8; depth = encoder blocks). Trainer > IQA "
                                                "pretrains and benchmarks these from dataset folders.")

    ckpt_path = os.path.join(model_registry.model_dir("personal_iqa", "iqa"), "scorer.pt")
    state = {"busy": False, "text": "", "last": None, "metrics": None}

    def _cap(cap, provider=None):
        try:
            return host.request_model(cap, provider=provider or None)
        except NoProviderError:
            return None

    def _cache_key():
        return f"{host.config.get('personal_iqa_encoder') or host.broker.selected_id('embed')}|" \
               f"{host.config.get('personal_iqa_base')}|{GRID}"

    # ── expensive, image-only parts (cached on mtime+key) ────────────────
    def _encode(img):
        enc = _cap("embed", host.config.get("personal_iqa_encoder"))
        embed, tiles = [], []
        if enc:
            try:
                v = enc(core.object_grouping.downscale_to_cap(img))
                embed = [float(x) for x in v] if v is not None else []
                H, W = img.shape[:2]
                for gy in range(GRID):
                    for gx in range(GRID):
                        t = img[gy * H // GRID:(gy + 1) * H // GRID, gx * W // GRID:(gx + 1) * W // GRID]
                        v = enc(core.object_grouping.downscale_to_cap(t))
                        if v is not None:
                            tiles.append([float(x) for x in v])
            except Exception as e:
                host.logger.error(f"personal_iqa encode: {e}")
        base_q = None
        base = host.config.get("personal_iqa_base") or ""
        if base and base != "personal" and (fn := _cap("iqa", base)):
            try:
                base_q = (fn(img) or {}).get("quality")
            except Exception:
                pass
        return embed, tiles, base_q

    def _decode(rel_path):
        # "ext:<abs path>" = an image outside the library (iqa_train datasets);
        # it shares the feature cache (keyed on that string) but never joins ratings.
        fp = rel_path[4:] if rel_path.startswith("ext:") else host.safe_path(host.media_dir, rel_path)
        return core.to_bgr(core.read_image(fp))

    def _cached(db, rel_path, mtime):
        """Encoder/base outputs for one file; on miss decodes at FULL resolution, computes and stores."""
        key = _cache_key()
        row = db.execute("SELECT * FROM personal_iqa_cache WHERE rel_path=?", (rel_path,)).fetchone()
        if row and row["mtime"] == mtime and row["key"] == key:
            return json.loads(row["embed"]), json.loads(row["tiles"]), row["base_iqa"]
        embed, tiles, base_q = _encode(_decode(rel_path))
        db.execute("INSERT INTO personal_iqa_cache(rel_path,mtime,key,embed,tiles,base_iqa,trained) "
                   "VALUES(?,?,?,?,?,?,0) ON CONFLICT(rel_path) DO UPDATE SET mtime=excluded.mtime, "
                   "key=excluded.key, embed=excluded.embed, tiles=excluded.tiles, base_iqa=excluded.base_iqa",
                   (rel_path, mtime, key, json.dumps(embed), json.dumps(tiles), base_q))
        return embed, tiles, base_q

    # ── live parts (tags / faces / pose from where the app stores them) ──
    def _live(db, rel_path, img=None):
        out = {"face": [], "pose17": [], "pose133": [], "tags": net.hash_tags([])}
        ext = bool(rel_path) and rel_path.startswith("ext:")
        fp = host.safe_path(host.media_dir, rel_path) if rel_path and not ext else None
        pose, done = None, False
        if ext:
            # dataset image: faces/pose live only in our cache, computed once below
            r = db.execute("SELECT extra FROM personal_iqa_cache WHERE rel_path=?", (rel_path,)).fetchone()
            if r and r["extra"]:
                ex = json.loads(r["extra"])
                out["face"], pose, done = ex.get("face") or [], ex.get("pose"), bool(ex.get("done"))
        elif rel_path:
            r = db.execute("SELECT tags FROM files WHERE rel_path=?", (rel_path,)).fetchone()
            if r and r["tags"]:
                out["tags"] = net.hash_tags([common.tag_name(t) for t in json.loads(r["tags"])])
            for r in db.execute("SELECT shape FROM face_regions WHERE rel_path=? AND shape IS NOT NULL "
                                "AND COALESCE(not_face,0)=0", (rel_path,)):
                out["face"].append(np.frombuffer(r["shape"], np.float32).tolist())
            try:
                pose = core.read_metadata(fp).get("pose")
            except Exception:
                pass
        need = (not out["face"] or not pose) and not done
        if img is None and rel_path and need:
            try:
                img = _decode(rel_path)          # unscanned file: compute the missing parts on the fly
            except Exception:
                pass
        if img is not None and need:
            small = core.object_grouping.downscale_to_cap(img)
            if not out["face"] and (fn := _cap("detect.faces")):        # no face scan yet: compute, don't store
                try:
                    if boxes := fn(small):
                        _, _, shapes = core.embed_faces(small, boxes, want_shape=True)
                        out["face"] = [s.tolist() for s in shapes if s is not None]
                except Exception:
                    pass
            if not pose and (fn := _cap("pose")):                          # follows the user's pose pick
                try:
                    pose = {"people": fn(small)}
                except Exception:
                    pass
            if ext:
                db.execute("UPDATE personal_iqa_cache SET extra=? WHERE rel_path=?",
                           (json.dumps({"face": out["face"], "pose": pose, "done": True}), rel_path))
        for p in (pose or {}).get("people") or []:
            kps = p.get("keypoints") or []
            if len(kps) >= 17:
                out["pose17" if len(kps) == 17 else "pose133"].append(_norm_pose(kps))
        return out

    def features(db, rel_path, mtime=None, img=None):
        """All tokens for one image. With rel_path: stored tags/faces/pose + cached encoder output;
        img (may be downscaled) only serves as a fallback for missing faces/pose."""
        embed, tiles, base_q = _cached(db, rel_path, mtime) if rel_path else _encode(img)
        s = _live(db, rel_path, img)
        s.update(embed=[embed] if embed else [], tile=tiles, iqa=[[float(base_q)]] if base_q is not None else [])
        s["_base"] = base_q
        s["_missing"] = missing_parts(s, required())
        return s

    # ── checkpoint ───────────────────────────────────────────────────────
    def _ckpt():
        return torch.load(ckpt_path, map_location="cpu") if os.path.exists(ckpt_path) else None

    def _load_model():
        ck = _ckpt()
        if not ck:
            return None
        m = net.Scorer(ck["dims"], ck["d"], ck["depth"]); m.load_state_dict(ck["state"])
        return m.eval().to(model_registry.device())

    def _save(m, metrics, path=None):
        torch.save({"dims": m.dims, "d": m.d, "depth": m.depth, "metrics": metrics,
                    "state": m.cpu().state_dict()}, path or ckpt_path)
        if not path or path == ckpt_path:
            model_registry.unload(_KEY)
            state["metrics"] = metrics

    def _metrics():
        if state["metrics"] is None and _HAVE_TORCH:
            state["metrics"] = ((_ckpt() or {}).get("metrics")) or {}
        return state["metrics"] or {}

    def _available():
        mt = _metrics()
        return bool(mt) and mt.get("val_spearman", 0) > mt.get("base_spearman", 0)

    # ── training pass (background thread) ────────────────────────────────
    def _eval(model, samples, dev):
        model.eval(); pred = []
        with torch.no_grad():
            for i in range(0, len(samples), BATCH):
                f, mk, t = net.batch([s["feats"] for s in samples[i:i + BATCH]], model.dims, dev)
                pred += torch.sigmoid(model(f, mk, t)).tolist()
        y = [s["y"] for s in samples]
        base = [(s["feats"]["_base"], s["y"]) for s in samples if s["feats"]["_base"] is not None]
        mse = lambda a, b: float(np.mean((np.array(a) - np.array(b)) ** 2)) if a else None
        return {"n_val": len(samples), "val_mse": mse(pred, y), "val_spearman": net.spearman(pred, y),
                "base_mse": mse([b[0] for b in base], [b[1] for b in base]),
                "base_spearman": net.spearman([b[0] for b in base], [b[1] for b in base]) if base else 0.0}

    def _train():
        db = host.db()
        try:
            rows = db.execute(
                "SELECT r.rel_path, r.user_stars, f.mtime, c.trained, c.mtime cm, c.key "
                "FROM ratings r JOIN files f USING(rel_path) LEFT JOIN personal_iqa_cache c USING(rel_path) "
                "WHERE r.user_stars IS NOT NULL").fetchall()
            if len(rows) < MIN_RATINGS:
                state["text"] = f"Personal IQA: need at least {MIN_RATINGS} rated images ({len(rows)} now)."; return
            key = _cache_key()
            train, val, skipped = [], [], {}
            for i, r in enumerate(rows):
                state["text"] = f"[Personal IQA] features {i+1}/{len(rows)}"
                try:
                    fe = features(db, r["rel_path"], r["mtime"])
                except Exception as e:
                    host.logger.warning(f"personal_iqa features {r['rel_path']}: {e}"); continue
                if fe["_missing"]:
                    for m in fe["_missing"]:
                        skipped[m] = skipped.get(m, 0) + 1
                    continue
                fresh = bool(r["trained"]) and r["cm"] == r["mtime"] and r["key"] == key
                s = {"rel": r["rel_path"], "feats": fe, "y": r["user_stars"] / 5.0, "seen": fresh}
                (val if _is_val(r["rel_path"]) else train).append(s)
                if i % 25 == 0: db.commit()
            db.commit()
            if not train or not val:
                state["text"] = ("Personal IQA: not enough complete images for a train/val split "
                                 f"(skipped for missing {skipped})."); return

            dev = model_registry.device()
            model = _load_model()
            d, depth = net.tier_for(len(rows))
            dims = {"embed": len(train[0]["feats"]["embed"][0]) if train[0]["feats"]["embed"] else 1}
            dims["tile"] = dims["embed"]
            dims.update(face=215, pose17=34, pose133=266, iqa=1)
            # Only rebuild when the tier is BIGGER than the checkpoint and growing is
            # off; a pretrained (iqa_train) model larger than the tier is kept as is.
            rebuild = model is None or (d, depth) > (model.d, model.depth) and not host.config.get("personal_iqa_grow")
            if rebuild:
                model = net.Scorer(dims, d, depth).to(dev)
                batch, epochs = list(train), EPOCHS * 2
            else:
                if (d, depth) != (model.d, model.depth):
                    state["text"] = f"[Personal IQA] growing to D={d} depth={depth}"
                    model = model.grow(max(d, model.d), max(depth, model.depth)).to(dev)
                new = [s for s in train if not s["seen"]]
                old = [s for s in train if s["seen"]]
                batch = new + random.sample(old, min(len(old), REPLAY_RATIO * max(len(new), 1)))
                epochs = EPOCHS

            opt = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=0.01)
            for ep in range(epochs):
                model.train(); random.shuffle(batch); tot = 0.0
                for i in range(0, len(batch), BATCH):
                    chunk = batch[i:i + BATCH]
                    f, mk, t = net.batch([s["feats"] for s in chunk], model.dims, dev)
                    y = torch.tensor([s["y"] for s in chunk], device=dev)
                    loss = torch.nn.functional.mse_loss(torch.sigmoid(model(f, mk, t)), y)
                    opt.zero_grad(); loss.backward(); opt.step(); tot += loss.item() * len(chunk)
                state["text"] = f"[Personal IQA] epoch {ep+1}/{epochs} train mse={tot/len(batch):.4f}"
            metrics = _eval(model, val, dev)
            metrics.update(n_train=len(batch), n_ratings=len(rows), d=model.d, depth=model.depth,
                           rebuilt=rebuild, trained_at=time.time(), skipped=skipped)
            _save(model, metrics)
            db.execute("UPDATE personal_iqa_cache SET trained=1 WHERE rel_path IN (%s)"
                       % ",".join("?" * len(batch)), [s["rel"] for s in batch])
            db.commit()
            state["last"] = time.time()
            ok = metrics["val_spearman"] > metrics["base_spearman"]
            state["text"] = (f"Personal IQA: val spearman {metrics['val_spearman']:.3f} vs base "
                             f"{metrics['base_spearman']:.3f}, val mse {metrics['val_mse']:.4f} — "
                             + ("enabled" if ok else "NOT better than base yet; provider stays disabled"))
        except Exception as e:
            host.logger.error(f"personal_iqa train: {e}")
            state["text"] = f"Personal IQA: training failed — {e}"
        finally:
            core.db_close(); state["busy"] = False

    # ── service for iqa_train (pretraining from dataset folders) ────────
    def _fit(train, val, d, depth, epochs=10, batch=BATCH, lr=LR, say=None, stop=None):
        """Fresh Scorer(d, depth) fitted on samples [{feats, y}], evaluated on val.
        Returns (model, metrics)."""
        dev = model_registry.device()
        dims = {"embed": len(train[0]["feats"]["embed"][0]) if train[0]["feats"]["embed"] else 1}
        dims["tile"] = dims["embed"]
        dims.update(face=215, pose17=34, pose133=266, iqa=1)
        model = net.Scorer(dims, d, depth).to(dev)
        opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
        order = list(train)
        for ep in range(int(epochs)):
            model.train(); random.shuffle(order); tot = 0.0
            for i in range(0, len(order), batch):
                if stop is not None and stop.is_set():
                    raise RuntimeError("stopped")
                chunk = order[i:i + batch]
                f, mk, t = net.batch([s["feats"] for s in chunk], model.dims, dev)
                y = torch.tensor([s["y"] for s in chunk], device=dev)
                loss = torch.nn.functional.mse_loss(torch.sigmoid(model(f, mk, t)), y)
                opt.zero_grad(); loss.backward(); opt.step(); tot += loss.item() * len(chunk)
            if say:
                say(ep + 1, tot / max(1, len(order)))
        metrics = _eval(model, val, dev) if val else {}
        metrics.update(n_train=len(train), d=d, depth=depth, trained_at=time.time())
        return model, metrics

    host.provide_service("personal_iqa", {
        "features": features, "fit": _fit, "save": _save, "ckpt_path": ckpt_path,
        "ckpt_dir": os.path.dirname(ckpt_path), "metrics": _metrics,
        "reload": lambda: (model_registry.unload(_KEY), state.update(metrics=None)) and True,
        "sizes": lambda: net.parse_sizes(host.config.get("personal_iqa_sizes")),
        "required": required,
        "detectors": lambda: {"embed": bool(_cap("embed", host.config.get("personal_iqa_encoder"))),
                              "iqa": bool(_cap("iqa", host.config.get("personal_iqa_base") or None)),
                              "face": bool(_cap("detect.faces")), "pose": bool(_cap("pose"))},
        "count_params": net.count_params, "Scorer": net.Scorer, "batch": net.batch,
    })

    # ── iqa provider ─────────────────────────────────────────────────────
    model_registry.register(_KEY, _load_model, cost_mb=400, gpu=model_registry.on_gpu())

    def _scorer():
        def run(img_bgr, *a, rel_path=None, **k):
            mdl = model_registry.acquire(_KEY)
            if mdl is None or img_bgr is None:
                return {"raw": None, "quality": None}
            db = host.db()
            mtime = None
            if rel_path:
                r = db.execute("SELECT mtime FROM files WHERE rel_path=?", (rel_path,)).fetchone()
                mtime = r["mtime"] if r else None
            fe = features(db, rel_path, mtime, img_bgr)
            db.commit()
            f, mk, t = net.batch([fe], mdl.dims, model_registry.device())
            with torch.no_grad():
                q = float(torch.sigmoid(mdl(f, mk, t)).item())
            return {"raw": q, "quality": q}
        return run

    def _iqa_options():
        return [{"value": p["id"], "label": p["label"]}
                for p in host.broker.providers_for("iqa") if p["id"] != "personal"]

    def _enc_options():
        return [{"value": "", "label": "(selected embedding model)"}] + \
               [{"value": p["id"], "label": p["label"]} for p in host.broker.providers_for("embed")]

    host.provide_model(
        "iqa", "personal", label="Personal (learned from my ratings)", family="personal_iqa",
        speed="balanced",
        note="Predicts YOUR taste. Rate ≥50 images, Retrain in Settings ▸ Personal IQA; enabled once it beats the base model on validation.",
        loader=_scorer, available=_available,
        reason="not trained yet, or not better than the base IQA on validation — see Settings ▸ Personal IQA",
        settings=[{"key": "personal_iqa_base", "label": "Base IQA model (feature)", "kind": "select", "options": _iqa_options},
                  {"key": "personal_iqa_encoder", "label": "Encoder for image/tiles", "kind": "select", "options": _enc_options},
                  {"key": "personal_iqa_grow", "label": "Grow (Net2Net) instead of rebuild on tier change", "kind": "toggle"}],
        cost_mb=400, gpu=model_registry.on_gpu())

    # ── endpoints ────────────────────────────────────────────────────────
    def api_train():
        if not _HAVE_TORCH:
            return jsonify({"success": False, "error": "torch not installed"})
        if state["busy"]:
            return jsonify({"success": False, "error": "training already running"})
        state["busy"] = True; state["text"] = "[Personal IQA] starting…"
        threading.Thread(target=_train, daemon=True).start()
        return jsonify({"success": True})

    def api_status():
        db = host.db()
        n = db.execute("SELECT COUNT(*) c FROM ratings WHERE user_stars IS NOT NULL").fetchone()["c"]
        d, depth = net.tier_for(n)
        metrics = _metrics() or None
        return jsonify({"success": True, "ratings": n, "min_ratings": MIN_RATINGS, "tier": {"d": d, "depth": depth},
                        "metrics": metrics, "available": _available(), "busy": state["busy"],
                        "text": state["text"], "torch": _HAVE_TORCH})

    host.add_route("/api/personal_iqa/status", api_status)
    host.add_route("/api/personal_iqa/train", api_train, methods=["POST"], feature="ai.iqa", level="write")
    def _file_deleted(rel_path):
        db = host.db()
        try:
            db.execute("DELETE FROM personal_iqa_cache WHERE rel_path=?", (rel_path,))
            db.commit()
        except Exception:
            pass
    host.on("file.deleted", _file_deleted)
    host.logger.info("personal_iqa: registered iqa provider 'personal'")