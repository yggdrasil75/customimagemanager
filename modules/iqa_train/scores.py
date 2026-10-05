"""
Engagement counts -> a 0..1 rating, for images that carry booru-style numbers
instead of a human score: tags "score_up: 12", "score_down: 3", "views: 4100",
"source: e621" (or a labels.csv with score_up/score_down/views[/source] columns).

Needs score_down or views; uses both when present. Scales differ per site
(an e621 score of 50 is ordinary, a safebooru score of 50 is top-1%), so the
two raw signals are turned into percentile ranks WITHIN their source and the
ranks averaged. ponytail: no site calibration tables to maintain; rank within
the batch you actually have. Post age is ignored (old posts accumulate), so
grab posts from a comparable window when you scrape.

  vote  = Wilson lower bound of up/(up+down)   (needs score_down)
  reach = (up + 1) / (views + 10)               (needs views; +10 damps tiny counts)
With fewer than MIN_RANK rows carrying a signal in a source, ranking is
meaningless, so the raw value is used instead: vote as is (already 0..1),
reach squashed as 1 - 1/(1 + 50*reach).
"""
import math
import re

_NUM = re.compile(r"^\s*(score_up|score_down|views|source|site)\s*[:=]\s*(.+?)\s*$", re.I)
_KEYS = {"score_up": "up", "score_down": "down", "views": "views", "source": "source", "site": "source"}


def parse_tags(tag_names):
    """Tag strings -> {up, down, views, source} (only the keys present)."""
    out = {}
    for t in tag_names:
        m = _NUM.match(str(t))
        if not m:
            continue
        k, v = _KEYS[m.group(1).lower()], m.group(2)
        if k == "source":
            out[k] = v.lower()
        else:
            try:
                out[k] = float(v.replace(",", ""))
            except ValueError:
                pass
    return out


def wilson(up, down, z=1.96):
    n = up + down
    if n <= 0:
        return 0.0
    p = up / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / denom


MIN_RANK = 5


def usable(r):
    return r.get("up") is not None and (r.get("down") is not None or r.get("views"))


def estimate(rows):
    """rows: [{"key", "up", "down"?, "views"?, "source"?}] -> {key: rating 0..1}.
    Rows lacking down and views are dropped."""
    rows = [r for r in rows if usable(r)]
    by_src = {}
    for r in rows:
        by_src.setdefault(r.get("source") or "unknown", []).append(r)
    out = {}
    for group in by_src.values():
        signals = []
        for r in group:
            s = {}
            if r.get("down") is not None:
                s["vote"] = wilson(r["up"], r["down"])
            if r.get("views"):
                s["reach"] = (r["up"] + 1) / (r["views"] + 10)
            signals.append(s)
        ranks = {}
        for name in ("vote", "reach"):
            vals = [(s[name], i) for i, s in enumerate(signals) if name in s]
            if not vals:
                continue
            if len(vals) < MIN_RANK:
                for v, i in vals:
                    ranks.setdefault(i, []).append(v if name == "vote" else 1 - 1 / (1 + 50 * v))
                continue
            vals.sort()
            n = len(vals)
            for pos, (_, i) in enumerate(vals):
                ranks.setdefault(i, []).append(pos / (n - 1))
        for i, r in enumerate(group):
            out[r["key"]] = sum(ranks[i]) / len(ranks[i])
    return out


if __name__ == "__main__":   # self-check
    assert parse_tags(["score_up: 12", "score_down:3", "views = 4,100", "source: e621", "cat"]) == \
        {"up": 12.0, "down": 3.0, "views": 4100.0, "source": "e621"}
    assert wilson(10, 0) > wilson(1, 0) > wilson(0, 0) == 0.0
    rows = [{"key": "a", "up": 50, "down": 2, "views": 1000, "source": "e621"},
            {"key": "b", "up": 5, "down": 5, "views": 1000, "source": "e621"},
            {"key": "c", "up": 1, "down": 0, "views": 20, "source": "e621"},
            {"key": "d", "up": 3, "down": 0, "source": "safebooru"},
            {"key": "e", "up": 90, "views": 100, "source": "safebooru"},
            {"key": "f", "up": 7}]                                  # no down, no views -> dropped
    y = estimate(rows)
    assert "f" not in y and y["a"] > y["b"] and 0 <= y["c"] <= 1
    assert y["d"] == wilson(3, 0) and abs(y["e"] - (1 - 1 / (1 + 50 * 91 / 110))) < 1e-9   # raw values below MIN_RANK
    big = [{"key": i, "up": i, "down": 100 - i, "views": 1000, "source": "x"} for i in range(20)]
    yb = estimate(big); assert yb[19] == 1.0 and yb[0] == 0.0                              # ranked above it
    print("ok")