"""training_validate.py — compare a trained model's predictions against the
ground-truth boxes on the same images, classify the differences, and score it.

Per image we greedily match predicted boxes to ground-truth boxes of the SAME
class by highest IoU, then label each box:

    correct    matched, IoU >= iou_ok
    tightened  matched, IoU in [iou_min, iou_ok), prediction is smaller than GT
    loosened   matched, IoU in [iou_min, iou_ok), prediction is larger than GT
    shifted    matched, IoU in [iou_min, iou_ok), similar area (moved, not resized)
    dropped    a GT box with no prediction (model missed it)
    added      a prediction with no GT box (model invented it)

Accuracy is reported two ways so callers can pick a bound:
    mean_iou   average IoU over matched pairs (0..1)
    f1         detection F1 at iou_ok (added=FP, dropped=FN, correct=TP)

Nothing here writes anything; it just reports. The trainer route decides what to
do with the diffs (show them, let the user confirm, maybe retrain).
"""


def _iou(a, b):
    """IoU of two center-form normalised boxes {cx,cy,w,h}."""
    ax1, ay1 = a["cx"] - a["w"] / 2, a["cy"] - a["h"] / 2
    ax2, ay2 = a["cx"] + a["w"] / 2, a["cy"] + a["h"] / 2
    bx1, by1 = b["cx"] - b["w"] / 2, b["cy"] - b["h"] / 2
    bx2, by2 = b["cx"] + b["w"] / 2, b["cy"] + b["h"] / 2
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / ua if ua > 0 else 0.0


def _area(b):
    return max(1e-9, b["w"] * b["h"])


def diff_image(gt, pred, iou_ok=0.7, iou_min=0.3, area_tol=0.15):
    """Compare one image's ground-truth vs predicted boxes.

    Returns {boxes:[...], counts:{...}, matched:[(iou)], mean_iou}. Each box entry
    carries enough to draw it and to explain the verdict.
    """
    gt = list(gt or [])
    pred = list(pred or [])
    used_pred = set()
    boxes = []
    counts = {"correct": 0, "tightened": 0, "loosened": 0, "shifted": 0,
              "dropped": 0, "added": 0}
    ious = []

    # Greedy: for each GT box, take the best-IoU unused prediction of same class.
    for gi, g in enumerate(gt):
        gname = (g.get("class_name") or "").strip()
        best_j, best_iou = -1, 0.0
        for j, p in enumerate(pred):
            if j in used_pred:
                continue
            if (p.get("class_name") or "").strip() != gname:
                continue
            v = _iou(g, p)
            if v > best_iou:
                best_iou, best_j = v, j
        if best_j >= 0 and best_iou >= iou_min:
            used_pred.add(best_j)
            p = pred[best_j]
            ious.append(best_iou)
            if best_iou >= iou_ok:
                verdict = "correct"
            else:
                ar = _area(p) / _area(g)
                if ar < 1 - area_tol:
                    verdict = "tightened"   # prediction smaller than truth
                elif ar > 1 + area_tol:
                    verdict = "loosened"    # prediction larger than truth
                else:
                    verdict = "shifted"
            counts[verdict] += 1
            boxes.append({"verdict": verdict, "class_name": gname, "iou": round(best_iou, 3),
                          "gt": g, "pred": p})
        else:
            counts["dropped"] += 1
            boxes.append({"verdict": "dropped", "class_name": gname, "iou": 0.0,
                          "gt": g, "pred": None})

    # Any prediction not matched to a GT box is an addition.
    for j, p in enumerate(pred):
        if j in used_pred:
            continue
        counts["added"] += 1
        boxes.append({"verdict": "added", "class_name": (p.get("class_name") or "").strip(),
                      "iou": 0.0, "gt": None, "pred": p})

    mean_iou = sum(ious) / len(ious) if ious else (1.0 if not gt and not pred else 0.0)
    return {"boxes": boxes, "counts": counts, "mean_iou": round(mean_iou, 4)}


def aggregate(per_image, iou_ok=0.7):
    """Roll per-image diffs into a dataset score.

    TP = correct (matched at/above iou_ok); everything matched below counts as a
    localisation issue but still a detection (so it's a TP for F1, tracked
    separately as 'loc_issues'); FP = added; FN = dropped.
    """
    c = {"correct": 0, "tightened": 0, "loosened": 0, "shifted": 0,
         "dropped": 0, "added": 0}
    ious = []
    for r in per_image:
        for k in c:
            c[k] += r["counts"].get(k, 0)
        for b in r["boxes"]:
            if b["pred"] is not None and b["gt"] is not None:
                ious.append(b["iou"])

    tp = c["correct"] + c["tightened"] + c["loosened"] + c["shifted"]  # detected
    fp = c["added"]
    fn = c["dropped"]
    strict_tp = c["correct"]  # detected AND well-localised
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    strict_prec = strict_tp / (strict_tp + fp + (tp - strict_tp)) if (strict_tp + fp + (tp - strict_tp)) else 0.0
    mean_iou = sum(ious) / len(ious) if ious else 0.0
    loc_issues = c["tightened"] + c["loosened"] + c["shifted"]
    return {
        "counts": c,
        "mean_iou": round(mean_iou, 4),
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(f1, 4),
        "strict_precision": round(strict_prec, 4),
        "loc_issues": loc_issues,
        "n_gt": tp + fn,
        "n_pred": tp + fp,
    }