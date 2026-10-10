"""! @file
@brief Grouping algorithms of the library pipeline: pure functions over items.

An item is a dict the library facade builds per file:

    {"rel": "2024/a.jxl", "t": epoch or None, "lat": .., "lon": .. (or None),
     "place": {"city", "admin1", "country", "cc"} or {}, "people": [names],
     "tags": [names], "kind": "image" | "video", "rating": int or None,
     "folder": "2024", "albums": [names], "owner": username or None}

A group is {"id", "method", "files": [rel...], "name": "", "description": ""}
plus whatever a later node adds. Nothing here touches the app: the tests drive
it with plain dicts.
"""

import hashlib
import math
from collections import Counter

import numpy as np

EARTH_KM = 6371.0


def haversine_km(a, b):
    """! @brief Great-circle distance in km between (lat, lon) pairs."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def _pos(it):
    return (it["lat"], it["lon"]) if it.get("lat") is not None and it.get("lon") is not None else None


def group_id(method, files):
    """! @brief A stable id for a group: the method and its sorted members."""
    h = hashlib.sha1(("%s\n%s" % (method, "\n".join(sorted(files)))).encode("utf-8")).hexdigest()
    return "%s-%s" % (method, h[:10])


def make_group(method, files, **extra):
    """! @brief A group dict with its stable id."""
    g = {"id": group_id(method, files), "method": method, "files": list(files), "name": "", "description": ""}
    g.update(extra)
    return g


def by_owner(items):
    """! @brief Items split per owner (personal libraries never share a group)."""
    out = {}
    for it in items:
        out.setdefault(it.get("owner"), []).append(it)
    return list(out.values())


def _timed(items):
    return sorted((it for it in items if it.get("t") is not None), key=lambda it: (it["t"], it["rel"]))


# -- time + place --------------------------------------------------------------
def trips(items, gap_hours=36.0, min_files=5, home=None, home_km=0.0, max_km=0.0):
    """! @brief Trips: runs of photos taken close together in time, away from home.

    A new trip starts after `gap_hours` without a photo, at a photo inside the
    home radius (when `home` (lat, lon) and `home_km` are set; home photos join
    no trip), or at a jump of more than `max_km` from the previous geotagged
    photo when `max_km` > 0. Photos without GPS ride along with their time
    neighbours. Runs shorter than `min_files` are dropped.
    """
    out = []
    for part in by_owner(items):
        cur, last_t, last_pos = [], None, None

        def close():
            if len(cur) >= max(1, int(min_files)):
                out.append(make_group("trip", [i["rel"] for i in cur]))
        for it in _timed(part):
            pos = _pos(it)
            at_home = bool(home and home_km and pos and haversine_km(pos, home) <= home_km)
            gap = last_t is not None and (it["t"] - last_t) > gap_hours * 3600
            jump = bool(max_km and pos and last_pos and haversine_km(pos, last_pos) > max_km)
            if at_home or gap or jump:
                close()
                cur = []
                last_pos = None
            last_t = it["t"]
            if at_home:
                continue
            cur.append(it)
            if pos:
                last_pos = pos
        close()
    return out


def events(items, gap_minutes=120.0, min_files=5):
    """! @brief Events: bursts of photos with no pause longer than `gap_minutes`."""
    out = []
    for part in by_owner(items):
        cur, last_t = [], None
        for it in _timed(part):
            if last_t is not None and (it["t"] - last_t) > gap_minutes * 60:
                if len(cur) >= min_files:
                    out.append(make_group("event", [i["rel"] for i in cur]))
                cur = []
            cur.append(it)
            last_t = it["t"]
        if len(cur) >= min_files:
            out.append(make_group("event", [i["rel"] for i in cur]))
    return out


def places(items, level="city", per_year=False, min_files=5):
    """! @brief One group per place (city / admin1 / country), optionally per year."""
    out = []
    for part in by_owner(items):
        buckets = {}
        for it in part:
            p = (it.get("place") or {}).get(level)
            if not p:
                continue
            key = (p,)
            if per_year:
                if it.get("t") is None:
                    continue
                key = (p, _year(it["t"]))
            buckets.setdefault(key, []).append(it["rel"])
        for key, rels in sorted(buckets.items()):
            if len(rels) >= min_files:
                out.append(make_group("place", rels, place_key=list(key)))
    return out


def _year(t):
    return int(1970 + t // 31556952)


# -- people ----------------------------------------------------------------------
def people(items, mode="each", min_files=5, min_people=1):
    """! @brief Groups by the people in the photos.
    mode "each": one group per person; "same_set": one group per exact set of
    people (the same family, the same friends) with at least `min_people`.
    """
    out = []
    for part in by_owner(items):
        buckets = {}
        for it in part:
            names = sorted({p for p in (it.get("people") or []) if p})
            if mode == "same_set":
                if len(names) >= max(1, int(min_people)):
                    buckets.setdefault(tuple(names), []).append(it["rel"])
            else:
                for n in names:
                    buckets.setdefault((n,), []).append(it["rel"])
        for key, rels in sorted(buckets.items()):
            if len(rels) >= min_files:
                out.append(make_group("people", rels, people_key=list(key)))
    return out


# -- embeddings ------------------------------------------------------------------
def _unit(v):
    v = np.asarray(v, np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def similar(items, vecs, threshold=0.85, min_files=5, window_hours=0.0):
    """! @brief Look-alike groups from image embeddings (cosine >= threshold).

    With `window_hours` > 0 only photos taken within that window of each other
    are compared (cheap: sorted by time, a sliding window) and linked into
    connected components; without it a single pass assigns each photo to the
    most similar group centroid above the threshold (leader clustering), which
    stays linear in the library size.
    """
    out = []
    for part in by_owner(items):
        have = [it for it in part if it["rel"] in vecs]
        if window_hours and window_hours > 0:
            seq = _timed(have)
            parent = list(range(len(seq)))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i
            unit = [_unit(vecs[it["rel"]]) for it in seq]
            lo = 0
            for i, it in enumerate(seq):
                while seq[lo]["t"] < it["t"] - window_hours * 3600:
                    lo += 1
                for j in range(lo, i):
                    if float(np.dot(unit[i], unit[j])) >= threshold:
                        parent[find(i)] = find(j)
            comps = {}
            for i, it in enumerate(seq):
                comps.setdefault(find(i), []).append(it["rel"])
            groups = list(comps.values())
        else:
            cents, members = [], []
            for it in sorted(have, key=lambda x: x["rel"]):
                v = _unit(vecs[it["rel"]])
                best, best_s = -1, threshold
                for k, c in enumerate(cents):
                    s = float(np.dot(v, _unit(c)))
                    if s >= best_s:
                        best, best_s = k, s
                if best < 0:
                    cents.append(v.copy())
                    members.append([it["rel"]])
                else:
                    n = len(members[best])
                    cents[best] = (cents[best] * n + v) / (n + 1)
                    members[best].append(it["rel"])
            groups = members
        for rels in groups:
            if len(rels) >= min_files:
                out.append(make_group("similar", rels))
    return out


def centroid(vecs, rels):
    """! @brief The mean unit vector of a group's members that have one (None if none)."""
    vs = [_unit(vecs[r]) for r in rels if r in vecs]
    if not vs:
        return None
    return _unit(np.mean(vs, axis=0))


def merge_similar(groups, items_by_rel, vecs, threshold=0.8, max_gap_hours=72.0):
    """! @brief Join neighbouring groups that look alike and are close in time (a trip
    split by a quiet day). Groups are taken in time order; each one is merged into
    the previous when their centroids reach `threshold` and the gap between them
    is at most `max_gap_hours`."""
    def span(g):
        ts = [items_by_rel[r]["t"] for r in g["files"] if r in items_by_rel and items_by_rel[r].get("t") is not None]
        return (min(ts), max(ts)) if ts else (None, None)
    ordered = sorted(groups, key=lambda g: (span(g)[0] is None, span(g)[0] or 0))
    out = []
    for g in ordered:
        if out:
            prev = out[-1]
            (_, pe), (gs, _) = span(prev), span(g)
            ca, cb = centroid(vecs, prev["files"]), centroid(vecs, g["files"])
            close = pe is not None and gs is not None and (gs - pe) <= max_gap_hours * 3600
            if close and ca is not None and cb is not None and float(np.dot(ca, cb)) >= threshold:
                files = prev["files"] + [r for r in g["files"] if r not in set(prev["files"])]
                merged = dict(prev, files=files)
                merged["id"] = group_id(prev["method"], files)
                out[-1] = merged
                continue
        out.append(g)
    return out


# -- describing a group ----------------------------------------------------------------
def stats(group, items_by_rel, top=5):
    """! @brief What a group is, in numbers: dates, places, people, tags, GPS share."""
    its = [items_by_rel[r] for r in group["files"] if r in items_by_rel]
    ts = sorted(it["t"] for it in its if it.get("t") is not None)
    pos = [_pos(it) for it in its if _pos(it)]
    cities, countries, admins, ppl, tags = Counter(), Counter(), Counter(), Counter(), Counter()
    for it in its:
        pl = it.get("place") or {}
        if pl.get("city"):
            cities[pl["city"]] += 1
        if pl.get("admin1"):
            admins[pl["admin1"]] += 1
        if pl.get("country"):
            countries[pl["country"]] += 1
        for p in it.get("people") or []:
            ppl[p] += 1
        for t in it.get("tags") or []:
            tags[t] += 1
    out = {"count": len(group["files"]), "start": ts[0] if ts else None, "end": ts[-1] if ts else None,
           "days": (int((ts[-1] - ts[0]) // 86400) + 1) if ts else 0,
           "cities": [c for c, _ in cities.most_common(top)],
           "regions": [c for c, _ in admins.most_common(top)],
           "countries": [c for c, _ in countries.most_common(top)],
           "people": [p for p, _ in ppl.most_common(top)],
           "tags": [t for t, _ in tags.most_common(top)],
           "gps": round(len(pos) / len(its), 2) if its else 0.0,
           "videos": sum(1 for it in its if it.get("kind") == "video")}
    if pos:
        out["center"] = [round(sum(p[0] for p in pos) / len(pos), 5), round(sum(p[1] for p in pos) / len(pos), 5)]
    return out


def album_overlap(group_files, album_members):
    """! @brief The album holding most of the group: (album, share of the group in it)."""
    files = set(group_files)
    best, share = None, 0.0
    if not files:
        return best, share
    for name, members in album_members.items():
        s = len(files & members) / len(files)
        if s > share:
            best, share = name, s
    return best, share


def sample(files, n):
    """! @brief `n` members spread evenly over the group (in its order)."""
    if n <= 0 or not files:
        return []
    if len(files) <= n:
        return list(files)
    step = (len(files) - 1) / float(n - 1) if n > 1 else 0
    return [files[int(round(i * step))] for i in range(n)]