"""
Train Personal IQA scorer sizes from a feature pack, with nothing but torch.
======================================================================
    python modules/iqa_train/train_pack.py PACK.pt [PACK2.pt ...] --out DIR
        [--sizes "name d depth" ...] [--epochs 20] [--batch 256] [--lr 1e-3]
        [--limit N] [--ablate SIZE] [--device cuda]

Loads one or more packs written by iqa_train.pack (samples already split
train/val by key hash), fits every size on the same samples, reports val
MSE + Spearman vs the base IQA, params and ms/image, and writes
<out>/scorer_<size>.pt in the exact checkpoint format the personal provider
loads (copy one to models/personal_iqa/iqa/scorer.pt, or scorer_<size>.pt
beside it and Activate in Trainer > IQA). --ablate SIZE refits that size
once per token type with the type dropped: the Spearman delta per type is
what each feature is worth, so the extraction pipeline can be trimmed.
--limit N trains on the first N samples (local validation of the run before
renting the big box). Only this file and modules/personal_iqa/net.py are
needed on the training machine, plus torch and numpy.
"""
import argparse
import importlib.util
import json
import os
import random
import sys
import time

import numpy as np
import torch

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("piqa_net", os.path.join(_HERE, "..", "personal_iqa", "net.py"))
net = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(net)


def load_packs(paths, limit=None):
    train, val, dims, meta = [], [], {}, []
    for p in paths:
        pk = torch.load(p, map_location="cpu", weights_only=False)
        train += pk["train"]; val += pk["val"]
        for k, v in pk["dims"].items():     # a later pack may have a wider text encoder etc.; take the max
            dims[k] = max(dims.get(k, 1), v)
        meta.append({"path": p, "n_train": len(pk["train"]), "n_val": len(pk["val"]),
                     "profile": pk.get("profile"), "detectors": pk.get("detectors")})
    if limit:
        random.Random(0).shuffle(train); train = train[:int(limit)]
        val = val[:max(1, int(limit) // 5)]
    return train, val, dims, meta


def evaluate(model, samples, dev, batch):
    model.eval(); pred = []
    with torch.no_grad():
        for i in range(0, len(samples), batch):
            f, mk, t = net.batch([s["feats"] for s in samples[i:i + batch]], model.dims, dev)
            pred += torch.sigmoid(model(f, mk, t)).tolist()
    y = [s["y"] for s in samples]
    base = [(s["feats"].get("base"), s["y"]) for s in samples if s["feats"].get("base") is not None]
    mse = lambda a, b: float(np.mean((np.array(a) - np.array(b)) ** 2)) if a else None
    return {"n_val": len(samples), "val_mse": mse(pred, y), "val_spearman": net.spearman(pred, y),
            "base_mse": mse([b[0] for b in base], [b[1] for b in base]),
            "base_spearman": net.spearman([b[0] for b in base], [b[1] for b in base]) if base else 0.0}


def fit(train, val, dims, d, depth, epochs, batch, lr, dev, say=print):
    model = net.Scorer(dims, d, depth).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, lr, total_steps=max(1, epochs * ((len(train) + batch - 1) // batch)))
    order, best, t0 = list(train), None, time.time()
    for ep in range(epochs):
        model.train(); random.shuffle(order); tot = 0.0
        for i in range(0, len(order), batch):
            chunk = order[i:i + batch]
            f, mk, t = net.batch([s["feats"] for s in chunk], dims, dev)
            y = torch.tensor([s["y"] for s in chunk], device=dev)
            loss = torch.nn.functional.mse_loss(torch.sigmoid(model(f, mk, t)), y)
            opt.zero_grad(); loss.backward(); opt.step(); sched.step(); tot += loss.item() * len(chunk)
        m = evaluate(model, val, dev, batch) if val else {}
        say(f"  epoch {ep + 1}/{epochs} train mse {tot / max(1, len(order)):.4f}"
            + (f"  val mse {m['val_mse']:.4f} spearman {m['val_spearman']:.3f}" if m else ""))
        if m and (best is None or m["val_spearman"] > best[0]):      # keep the best epoch, not the last
            best = (m["val_spearman"], {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}, m, ep + 1)
    if best:
        model.load_state_dict(best[1]); metrics = dict(best[2], best_epoch=best[3])
    else:
        metrics = {}
    metrics.update(n_train=len(train), d=d, depth=depth, epochs=epochs, train_s=round(time.time() - t0, 1),
                   trained_at=time.time(), params=sum(p.numel() for p in model.parameters()))
    return model, metrics


def bench_ms(model, val, dev, batch):
    f, mk, t = net.batch([s["feats"] for s in val[:batch]], model.dims, dev)
    model.eval()
    with torch.no_grad():
        model(f, mk, t)
        if dev != "cpu":
            torch.cuda.synchronize()
        t0 = time.perf_counter()
        for _ in range(5):
            model(f, mk, t)
        if dev != "cpu":
            torch.cuda.synchronize()
    return round((time.perf_counter() - t0) / 5 / len(val[:batch]) * 1000, 3)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sizes", nargs="*", default=None, help='"name d depth" per entry; default: net.SIZES')
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--limit", type=int, default=None, help="train on N samples only (quick local check)")
    ap.add_argument("--ablate", default=None, help="size name: refit dropping one token type at a time")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    a = ap.parse_args(argv)
    torch.manual_seed(0); random.seed(0)
    sizes = net.parse_sizes("\n".join(a.sizes)) if a.sizes else net.SIZES
    train, val, dims, meta = load_packs(a.packs, a.limit)
    os.makedirs(a.out, exist_ok=True)
    report = {"packs": meta, "dims": dims, "n_train": len(train), "n_val": len(val), "device": a.device,
              "args": vars(a), "sizes": {}, "ablation": {}}
    print(f"{len(train)} train / {len(val)} val samples on {a.device}; dims {dims}")
    for m in meta:
        if m["profile"]:
            p = m["profile"]
            print(f"  {os.path.basename(m['path'])}: features {p.get('ms')} ms/image "
                  f"({p.get('images_computed')} computed, {p.get('images_cached')} cached) {p.get('ms_per_image')}")
    for z, sp in sizes.items():
        print(f"[{z}] d={sp['d']} depth={sp['depth']}")
        model, metrics = fit(train, val, dims, sp["d"], sp["depth"], a.epochs, a.batch, a.lr, a.device)
        metrics["ms_per_image"] = bench_ms(model, val, a.device, a.batch) if val else None
        report["sizes"][z] = metrics
        torch.save({"dims": model.dims, "d": model.d, "depth": model.depth, "metrics": metrics,
                    "state": model.cpu().state_dict()}, os.path.join(a.out, f"scorer_{z}.pt"))
        print(f"  -> val spearman {metrics.get('val_spearman', 0):.3f} (base {metrics.get('base_spearman', 0):.3f}) "
              f"mse {metrics.get('val_mse')}  params {metrics['params']}  {metrics['ms_per_image']} ms/img  "
              f"best epoch {metrics.get('best_epoch')}  {metrics['train_s']} s")
    if a.ablate and a.ablate in sizes:
        sp, full = sizes[a.ablate], report["sizes"][a.ablate]["val_spearman"]
        groups = {"embed": ["embed"], "tile": ["tile", "tile_raw"], "object": ["object", "object_raw"],
                  "region": ["region", "region_raw"], "face": ["face", "face_raw"],
                  "pose": ["pose17", "pose17_raw", "pose133", "pose133_raw", "bones"], "iqa": ["iqa"],
                  "tags": ["tags"], "tag_text": ["tag_text"], "style": ["style"], "depth": ["depth"],
                  "comp": ["comp"], "exif": ["exif"]}
        present = {g: ks for g, ks in groups.items() if any(s["feats"].get(k) for s in train[:500] for k in ks)}
        for g, ks in present.items():
            print(f"[ablate {a.ablate}] without {g}")
            def drop(s):
                fe = dict(s["feats"])
                for k in ks:
                    fe[k] = [0] if k == "tags" else []
                return {"y": s["y"], "feats": fe}
            _, m = fit([drop(s) for s in train], [drop(s) for s in val], dims, sp["d"], sp["depth"],
                       a.epochs, a.batch, a.lr, a.device, say=lambda *_: None)
            report["ablation"][g] = {"val_spearman": round(m["val_spearman"], 4), "delta": round(m["val_spearman"] - full, 4)}
            print(f"  -> spearman {m['val_spearman']:.3f}  delta {m['val_spearman'] - full:+.3f}")
        print("ablation, most valuable first (delta < 0 = the feature helps; ~0 = droppable from extraction):")
        for g, r in sorted(report["ablation"].items(), key=lambda kv: kv[1]["delta"]):
            print(f"  {g:9s} {r['delta']:+.3f}")
    with open(os.path.join(a.out, "report.json"), "w") as f:
        json.dump(report, f, indent=1, default=str)
    print(f"written {a.out}/scorer_<size>.pt + report.json")
    return report


if __name__ == "__main__":
    main()