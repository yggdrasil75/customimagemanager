"""
family_share: rule evaluation and outbox planning.
======================================================================
Pure logic over a sqlite connection — no Flask, no HTTP, no host — so it is
testable on its own and the module.py stays thin.

The question this file answers, for every (file, peer) pair:

    "Should THIS file be visible on THAT peer's instance?"

and the answer is deliberately conservative:

  * nothing is shared unless an enabled SHARE rule lists the peer and matches
    the file (by folder, album, or tag);
  * any enabled BLOCK rule that matches the file vetoes it, for every peer the
    block names (a block with no peers = everyone);
  * a file that arrived FROM a peer is never re-shared unless the user opts in;
  * tags only match when confirmed (an AI guess of "family" must not start
    sending photos around) unless the user opts in.

Rules live in fs_rules, peers in fs_peers, and the planner reconciles the
current answer against fs_outbox: new/changed pairs become 'pending', pairs
that used to be shared and no longer are become 'revoke'.
"""

import hashlib
import json
import os
import time

import common

DDL = """
CREATE TABLE IF NOT EXISTS fs_peers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL COLLATE NOCASE,   -- how the peer identifies itself
    url         TEXT NOT NULL DEFAULT '',              -- their base URL (https://mom.example:5000)
    key_out     TEXT NOT NULL DEFAULT '',              -- what we send them (their key_in)
    key_in      TEXT NOT NULL DEFAULT '',              -- what they must send us
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_ok     REAL,
    last_error  TEXT DEFAULT '',
    created     REAL
);
CREATE TABLE IF NOT EXISTS fs_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT DEFAULT '',
    mode        TEXT NOT NULL DEFAULT 'share',   -- share | block
    kind        TEXT NOT NULL,                   -- folder | album | tag
    value       TEXT NOT NULL,
    recursive   INTEGER NOT NULL DEFAULT 1,      -- folder rules: include subfolders
    peers       TEXT NOT NULL DEFAULT '[]',      -- JSON list of fs_peers.id ([] on a block = everyone)
    enabled     INTEGER NOT NULL DEFAULT 1,
    created     REAL
);
CREATE TABLE IF NOT EXISTS fs_outbox (
    rel_path    TEXT NOT NULL,
    peer_id     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | sent | revoke | error
    sig         TEXT DEFAULT '',                 -- signature of what the peer last got
    sha         TEXT DEFAULT '',                 -- sha256 the peer last got the bytes for
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT DEFAULT '',
    updated     REAL,
    PRIMARY KEY (rel_path, peer_id)
);
CREATE INDEX IF NOT EXISTS idx_fs_outbox_status ON fs_outbox(status, updated);
CREATE TABLE IF NOT EXISTS fs_received (
    origin_sha  TEXT NOT NULL,                   -- sha the SENDER reported for the bytes
    peer_id     INTEGER NOT NULL,                -- who sent it to us
    origin_id   TEXT DEFAULT '',                 -- instance id of the original owner
    rel_path    TEXT DEFAULT '',                 -- where it landed in our library
    queue_id    INTEGER DEFAULT 0,               -- upload_queue row while deferred
    albums      TEXT DEFAULT '[]',               -- albums still to apply once ingested
    received    REAL,
    PRIMARY KEY (origin_sha, peer_id)
);
CREATE INDEX IF NOT EXISTS idx_fs_received_path ON fs_received(rel_path);
"""

RULE_KINDS = ("folder", "album", "tag")
RULE_MODES = ("share", "block")


def _loads(s, default):
    try:
        v = json.loads(s or "")
        return v if v is not None else default
    except Exception:
        return default


def norm_folder(v):
    return str(v or "").strip().strip("/").replace("\\", "/")


def norm_rule(r):
    """Validate/normalise a rule dict from the UI. Raises ValueError."""
    mode = str(r.get("mode") or "share").strip().lower()
    kind = str(r.get("kind") or "").strip().lower()
    if mode not in RULE_MODES:
        raise ValueError("mode must be share or block")
    if kind not in RULE_KINDS:
        raise ValueError("kind must be folder, album or tag")
    value = norm_folder(r.get("value")) if kind == "folder" else str(r.get("value") or "").strip()
    if kind == "tag":
        value = common.tag_name(value)
    if not value and not (kind == "folder" and mode == "share" and r.get("value") == "/"):
        raise ValueError("value is required")
    peers = r.get("peers") or []
    if not isinstance(peers, list):
        raise ValueError("peers must be a list")
    peers = sorted({int(p) for p in peers})
    if mode == "share" and not peers:
        raise ValueError("a share rule needs at least one peer")
    return {
        "id": int(r["id"]) if r.get("id") else None,
        "name": str(r.get("name") or "")[:120],
        "mode": mode, "kind": kind, "value": value,
        "recursive": 1 if r.get("recursive", True) else 0,
        "peers": peers,
        "enabled": 1 if r.get("enabled", True) else 0,
    }


def row_rule(row):
    d = dict(row)
    d["peers"] = [int(p) for p in _loads(d.get("peers"), [])]
    return d


def load_rules(db, enabled_only=True):
    q = "SELECT * FROM fs_rules" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY mode DESC, id"
    return [row_rule(r) for r in db.execute(q).fetchall()]


def load_peers(db, enabled_only=True):
    q = "SELECT * FROM fs_peers" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name"
    return [dict(r) for r in db.execute(q).fetchall()]


# ── matching ────────────────────────────────────────────────────────────────

class FileFacts:
    """What the rules look at for one file."""
    __slots__ = ("rel_path", "folder", "tags", "albums", "sha", "description", "received")

    def __init__(self, rel_path, tags, albums, sha="", description="", received=False):
        self.rel_path = rel_path
        self.folder = os.path.dirname(rel_path).replace("\\", "/")
        self.tags = list(tags or [])
        self.albums = list(albums or [])
        self.sha = sha or ""
        self.description = description or ""
        self.received = bool(received)

    @classmethod
    def from_row(cls, row, received=False):
        return cls(row["rel_path"], _loads(row["tags"], []), _loads(row["albums"], []),
                   row["sha256"] or "", row["description"] or "", received)


def rule_matches(rule, facts, match_unconfirmed=False):
    kind, value = rule["kind"], rule["value"]
    if kind == "folder":
        if value == "":                      # the library root
            return True if rule.get("recursive", 1) else facts.folder == ""
        if facts.folder == value:
            return True
        return bool(rule.get("recursive", 1)) and facts.folder.startswith(value + "/")
    if kind == "album":
        return value in facts.albums
    if kind == "tag":
        want = value.lower()
        for t in facts.tags:
            if not match_unconfirmed and not common.tag_is_confirmed(t):
                continue
            if common.tag_name(t).lower() == want:
                return True
        return False
    return False


def peers_for_file(facts, rules, *, reshare_received=False, match_unconfirmed=False):
    """-> {peer_id: [reason strings]} for the peers that should get this file.

    Also returns the reasons a peer was vetoed under negative ids? No — vetoes
    are reported through explain(); this returns only the positive answer."""
    if facts.received and not reshare_received:
        return {}
    allowed, blocked = {}, set()
    for r in rules:
        if not r.get("enabled", 1):
            continue
        if not rule_matches(r, facts, match_unconfirmed):
            continue
        label = f"{r['mode']} {r['kind']} '{r['value'] or '/'}'" + (f" ({r['name']})" if r.get("name") else "")
        if r["mode"] == "block":
            if r["peers"]:
                blocked.update(r["peers"])
            else:
                return {}                      # block for everyone: nothing goes anywhere
            continue
        for p in r["peers"]:
            allowed.setdefault(p, []).append(label)
    return {p: why for p, why in allowed.items() if p not in blocked}


def explain(facts, rules, peers, *, reshare_received=False, match_unconfirmed=False):
    """Per-peer verdict with reasons, for the UI's "who sees this?"."""
    out = []
    matched = [r for r in rules if r.get("enabled", 1) and rule_matches(r, facts, match_unconfirmed)]
    positive = peers_for_file(facts, rules, reshare_received=reshare_received,
                              match_unconfirmed=match_unconfirmed)
    for p in peers:
        pid = p["id"]
        why = []
        if facts.received and not reshare_received:
            why.append("received from a peer; re-sharing is off")
        for r in matched:
            hits_peer = (not r["peers"]) if r["mode"] == "block" else (pid in r["peers"])
            if hits_peer or (r["mode"] == "block" and pid in r["peers"]):
                why.append(f"{r['mode']}: {r['kind']} '{r['value'] or '/'}'" + (f" ({r['name']})" if r.get("name") else ""))
        out.append({"peer_id": pid, "peer": p["name"], "shared": pid in positive,
                    "enabled": bool(p.get("enabled", 1)), "reasons": why})
    return out


def signature(facts, albums_sent):
    """What the peer currently holds for this file. Any change re-sends."""
    h = hashlib.sha1()
    h.update(facts.sha.encode()); h.update(b"\0")
    h.update(json.dumps(sorted(facts.tags)).encode()); h.update(b"\0")
    h.update(facts.description.encode("utf-8", "replace")); h.update(b"\0")
    h.update(json.dumps(sorted(albums_sent)).encode()); h.update(b"\0")
    h.update(os.path.dirname(facts.rel_path).encode("utf-8", "replace"))
    return h.hexdigest()


def albums_to_send(facts, rules, peer_id, share_all_albums=False):
    """Album names the peer is told about. By default only albums that a
    share rule for that peer names — an album's name is metadata too."""
    if share_all_albums:
        return sorted(facts.albums)
    named = {r["value"] for r in rules
             if r.get("enabled", 1) and r["mode"] == "share" and r["kind"] == "album"
             and peer_id in r["peers"]}
    return sorted(a for a in facts.albums if a in named)


# ── candidate scan ──────────────────────────────────────────────────────────

def _coarse_sql(rules):
    """A cheap WHERE that over-approximates every enabled share rule, so the
    planner doesn't walk a 200k-file library in Python on every pass. Block
    rules never widen the scan (they only remove). Returns (clause, params)
    or ("0", []) when no share rule is enabled."""
    ors, params = [], []
    for r in rules:
        if not r.get("enabled", 1) or r["mode"] != "share":
            continue
        if r["kind"] == "folder":
            if r["value"] == "":
                return "1", []
            if r.get("recursive", 1):
                ors.append("(rel_path LIKE ? ESCAPE '\\')")
                params.append(_like(r["value"]) + "/%")
            else:
                ors.append("(rel_path LIKE ? ESCAPE '\\' AND rel_path NOT LIKE ? ESCAPE '\\')")
                params += [_like(r["value"]) + "/%", _like(r["value"]) + "/%/%"]
        elif r["kind"] == "album":
            ors.append("(rel_path IN (SELECT rel_path FROM album_members WHERE album=?))")
            params.append(r["value"])
        elif r["kind"] == "tag":
            ors.append("(tags LIKE ? ESCAPE '\\')")
            params.append("%" + _like(json.dumps(r["value"])[1:-1]) + "%")
    if not ors:
        return "0", []
    return "(" + " OR ".join(ors) + ")", params


def _like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def received_paths(db):
    return {r["rel_path"] for r in db.execute(
        "SELECT rel_path FROM fs_received WHERE rel_path<>''").fetchall()}


def iter_candidates(db, rules):
    """Yield FileFacts for every file some share rule might apply to."""
    clause, params = _coarse_sql(rules)
    if clause == "0":
        return
    got = received_paths(db)
    q = ("SELECT rel_path, tags, albums, sha256, description FROM files "
         f"WHERE {clause} AND sha256 IS NOT NULL AND sha256<>'' ORDER BY rel_path")
    for row in db.execute(q, params):
        yield FileFacts.from_row(row, received=row["rel_path"] in got)


def facts_for(db, rel_path):
    row = db.execute("SELECT rel_path, tags, albums, sha256, description FROM files "
                     "WHERE rel_path=?", (rel_path,)).fetchone()
    if row is None:
        return None
    got = db.execute("SELECT 1 FROM fs_received WHERE rel_path=?", (rel_path,)).fetchone()
    return FileFacts.from_row(row, received=got is not None)


# ── planning ────────────────────────────────────────────────────────────────

def desired(db, cfg, rel_paths=None):
    """-> {(rel_path, peer_id): (sig, sha, albums, reasons)} — the complete
    current answer for either the whole library or a few paths."""
    rules = load_rules(db)
    peers = {p["id"] for p in load_peers(db)}
    reshare = bool(cfg.get("reshare_received"))
    unconf = bool(cfg.get("match_unconfirmed_tags"))
    share_all = bool(cfg.get("share_all_albums"))
    out = {}
    if rel_paths is None:
        it = iter_candidates(db, rules)
    else:
        it = (f for f in (facts_for(db, rp) for rp in rel_paths) if f is not None)
    for facts in it:
        for pid, why in peers_for_file(facts, rules, reshare_received=reshare,
                                       match_unconfirmed=unconf).items():
            if pid not in peers:
                continue
            albums = albums_to_send(facts, rules, pid, share_all)
            out[(facts.rel_path, pid)] = (signature(facts, albums), facts.sha, albums, why)
    return out


def plan(db, cfg, rel_paths=None):
    """Reconcile fs_outbox with desired(). Returns counts.

    rel_paths=None does the whole library; a list limits the pass to those
    files (what upload/index events do). A pair that stops being desired
    becomes 'revoke' when revoke_on_unshare is on, else it is just forgotten.
    """
    want = desired(db, cfg, rel_paths)
    now = time.time()
    revoke = bool(cfg.get("revoke_on_unshare", True))
    if rel_paths is None:
        rows = db.execute("SELECT rel_path, peer_id, status, sig, sha FROM fs_outbox").fetchall()
    else:
        rows = []
        for rp in rel_paths:
            rows += db.execute("SELECT rel_path, peer_id, status, sig, sha FROM fs_outbox "
                               "WHERE rel_path=?", (rp,)).fetchall()
    have = {(r["rel_path"], r["peer_id"]): r for r in rows}
    n_new = n_changed = n_revoke = n_drop = 0
    for key, (sig, sha, _albums, _why) in want.items():
        cur = have.get(key)
        if cur is None:
            db.execute("INSERT INTO fs_outbox(rel_path, peer_id, status, sig, sha, updated) "
                       "VALUES (?,?,'pending','','',?)", (key[0], key[1], now))
            n_new += 1
        elif cur["status"] in ("sent", "error") and cur["sig"] != sig:
            db.execute("UPDATE fs_outbox SET status='pending', attempts=0, error='', updated=? "
                       "WHERE rel_path=? AND peer_id=?", (now, key[0], key[1]))
            n_changed += 1
        elif cur["status"] == "revoke":       # un-revoked before it went out
            db.execute("UPDATE fs_outbox SET status='pending', attempts=0, error='', updated=? "
                       "WHERE rel_path=? AND peer_id=?", (now, key[0], key[1]))
            n_changed += 1
    for key, cur in have.items():
        if key in want:
            continue
        if cur["status"] in ("sent",) or (cur["status"] == "error" and cur["sha"]):
            if revoke:
                db.execute("UPDATE fs_outbox SET status='revoke', attempts=0, error='', updated=? "
                           "WHERE rel_path=? AND peer_id=?", (now, key[0], key[1]))
                n_revoke += 1
            else:
                db.execute("DELETE FROM fs_outbox WHERE rel_path=? AND peer_id=?", key)
                n_drop += 1
        elif cur["status"] == "pending" or (cur["status"] == "error" and not cur["sha"]):
            db.execute("DELETE FROM fs_outbox WHERE rel_path=? AND peer_id=?", key)
            n_drop += 1
    db.commit()
    return {"new": n_new, "changed": n_changed, "revoke": n_revoke, "dropped": n_drop,
            "desired": len(want)}


def preview(db, cfg, peer_id, limit=500):
    """What a peer would see right now, with the rule that lets each file
    through — the check the user runs before trusting a rule set."""
    out = []
    for (rp, pid), (_sig, _sha, albums, why) in sorted(desired(db, cfg).items()):
        if pid != peer_id:
            continue
        out.append({"rel_path": rp, "albums": albums, "reasons": why})
        if len(out) >= limit:
            break
    return out