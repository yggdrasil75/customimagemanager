"""! @file
@brief Stack grouping rules: pure functions, no app state.

Two automatic groupings feed the stacks module:

  raw     a developed camera raw and the camera's own rendering of the same
          shot (IMG_1234.CR2 next to IMG_1234.JPG). The developed image
          carries the raw's name (EXIF OriginalRawFileName, Camera Raw
          crs:RawFileName or the raws table), so the pair is found by name in
          the same folder, with the capture time as a guard against a camera
          counter that wrapped.
  burst   near-identical shots taken in quick succession. Candidates are the
          dedup module's similar groups; members over the similarity floor
          that share a folder or an album and whose capture times chain with
          gaps no larger than the max drift become one stack.
"""

import os
import re

## @brief Raw + rendering of one shot share a capture time; a counter that wrapped does not.
RAW_TIME_TOLERANCE_S = 2.0

_SUFFIX_RE = re.compile(r"_\d+$")


def name_key(name):
    """! @brief The match key of a file name: its stem, lowercased (directory and extension dropped)."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return os.path.splitext(base)[0].strip().lower()


def strip_copy_suffix(stem):
    """! @brief A stem without the "_<n>" the upload adds when a name is taken (case kept)."""
    return _SUFFIX_RE.sub("", stem)


def stem_keys(rel_path):
    """! @brief (key, key2) of a library file: its stem, and its stem without the "_<n>"
    the upload adds when the name was taken. key2 equals key when there is no suffix.
    """
    key = name_key(rel_path)
    return key, strip_copy_suffix(key)


def folder_of(rel_path):
    """! @brief The folder part of a rel_path ('' for the library root)."""
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""


class _UnionFind:
    """! @brief Minimal union-find over hashable items."""

    def __init__(self):
        """! @brief An empty forest."""
        self.parent = {}

    def find(self, x):
        """! @brief The root of x's set (x joins as its own set on first sight)."""
        p = self.parent.setdefault(x, x)
        while p != self.parent[p]:
            self.parent[p] = self.parent[self.parent[p]]
            p = self.parent[p]
        self.parent[x] = p
        return p

    def union(self, a, b):
        """! @brief Merge the sets of a and b."""
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self):
        """! @brief Every set as a list."""
        out = {}
        for x in list(self.parent):
            out.setdefault(self.find(x), []).append(x)
        return list(out.values())


def _time_ok(a, b, tol):
    """! @brief Two capture epochs are compatible: within tol, or either unknown."""
    if a is None or b is None:
        return True
    return abs(float(a) - float(b)) <= tol


def group_raw(rows, tolerance=RAW_TIME_TOLERANCE_S):
    """! @brief Raw + rendering groups.
    @param rows  iterable of dicts {rel_path, folder, key, key2, raw_key, epoch};
                 raw_key is the name key of the raw a developed image came from
                 ('' or None for a file that is not a developed raw).
    @return [(members, cover)] with members sorted, cover the camera rendering
            (a file that is not a developed raw) when there is one.
    """
    rows = list(rows)
    by_folder_key = {}
    for r in rows:
        for k in {r.get("key"), r.get("key2")}:
            if k:
                by_folder_key.setdefault((r.get("folder") or "", k), []).append(r)
    uf = _UnionFind()
    for r in rows:
        rk = r.get("raw_key")
        if not rk:
            continue
        for other in by_folder_key.get((r.get("folder") or "", rk), []):
            if other["rel_path"] == r["rel_path"]:
                continue
            if not _time_ok(r.get("epoch"), other.get("epoch"), tolerance):
                continue
            uf.union(r["rel_path"], other["rel_path"])
    info = {r["rel_path"]: r for r in rows}
    out = []
    for members in uf.groups():
        if len(members) < 2:
            continue
        members = sorted(members)
        rendered = [m for m in members if not info[m].get("raw_key")]
        cover = None
        if rendered:
            raw_keys = {info[m].get("raw_key") for m in members if info[m].get("raw_key")}
            exact = [m for m in rendered if info[m].get("key") in raw_keys]
            cover = (exact or rendered)[0]
        out.append((members, cover or members[0]))
    out.sort(key=lambda g: g[1])
    return out


def group_bursts(dedup_groups, info, max_drift, min_similarity, skip=()):
    """! @brief Burst groups from dedup similar groups.
    @param dedup_groups    iterable of {members, scores}; scores[i] is members[i]'s
                           similarity to members[0] (the reference), 0..1.
    @param info            {rel_path: {"folder", "albums" (iterable), "epoch"}}.
    @param max_drift       largest gap in seconds between consecutive shots.
    @param min_similarity  members below this similarity to the reference are left out.
    @param skip            rel_paths that may not join a burst (already stacked, opted out).
    @return [(members in capture order, cover)]; cover is the dedup reference when it
            made the burst, else the first shot.
    """
    skip = set(skip)
    out = []
    for g in dedup_groups:
        members = list(g.get("members") or [])
        scores = list(g.get("scores") or [])
        if len(scores) != len(members):
            scores = [1.0] * len(members)
        ref = members[0] if members else None
        keep = []
        for m, s in zip(members, scores):
            if m in skip or m not in info:
                continue
            if info[m].get("epoch") is None:
                continue
            if m != ref and (s is None or float(s) < float(min_similarity)):
                continue
            keep.append(m)
        if len(keep) < 2:
            continue
        # files that share a folder or an album belong together
        uf = _UnionFind()
        owner = {}
        for m in keep:
            uf.find(m)
            tags = [("f", info[m].get("folder") or "")]
            tags += [("a", a) for a in (info[m].get("albums") or ())]
            for t in tags:
                if t in owner:
                    uf.union(owner[t], m)
                else:
                    owner[t] = m
        for part in uf.groups():
            part.sort(key=lambda m: (float(info[m]["epoch"]), m))
            run = [part[0]]
            for prev, cur in zip(part, part[1:]):
                gap = float(info[cur]["epoch"]) - float(info[prev]["epoch"])
                if gap <= float(max_drift):
                    run.append(cur)
                    continue
                if len(run) >= 2:
                    out.append((run, ref if ref in run else run[0]))
                run = [cur]
            if len(run) >= 2:
                out.append((run, ref if ref in run else run[0]))
    return out


def auto_delays(epochs, default_ms=100, lo=40, hi=1000):
    """! @brief Frame delays (ms) from capture times: each frame lasts until the next
    shot, clamped to [lo, hi]; the last frame repeats the previous delay. Unknown or
    equal times fall back to default_ms.
    """
    n = len(epochs)
    if n == 0:
        return []
    out = []
    for a, b in zip(epochs, epochs[1:]):
        if a is None or b is None or float(b) <= float(a):
            out.append(default_ms)
        else:
            out.append(int(max(lo, min(hi, round((float(b) - float(a)) * 1000)))))
    out.append(out[-1] if out else default_ms)
    return out