"""! @file persons.py
@brief Unified, reusable person model, time-scoped, that outlives the source images.

A person record is a source-of-truth file under `<media>/.persons/<uuid>.person`,
a zip container. Its structure has two tiers because one person is not one body:
an 18-year-old and the same person at 60 are genuinely different shapes, and
averaging across them would blend two real bodies into one that matches neither.

  descriptor.json : the person (stable for life) — uuid, name, off-image bio
                    (birthday, relationships), and a list of APPEARANCES.
                    Each appearance is one stable era of the person's look:
                    its own body-description fields (hair greys, physique
                    changes), its own identity centroids (arcface/dino) computed
                    only from that era's photos, its image membership and a
                    descriptive date span.
  tpose_<id>.json : one appearance's canonical T-pose keypoints (optional).
  mesh_<id>.obj   : one appearance's canonical body mesh (optional).

Appearances are formed from face-embedding drift (a physical, monotonic process),
NOT from capture dates — scanned family photos routinely carry the scan date, not
the shot date, so a date is only ever validated against the embedding era and
flagged when it disagrees; it never moves a photo between eras.

The DB `persons` table is a disposable cache mapping cluster_id -> uuid; it is
rebuilt by scanning `.persons/`, so a lost DB costs only recompute, never the
descriptor/mesh/relationships that live nowhere else.

Body fields are filled by a secondary pipeline or an LLM action through the SAME
per-field store, into the SAME appearance the pipeline uses — that is the unification.
"""

import io
import json
import os
import uuid
import zipfile
from typing import Any, Optional

DESCRIPTOR = "descriptor.json"

def tpose_member(appearance_id: str) -> str:
    """! @brief Container member name for one appearance's canonical T-pose."""
    return f"tpose_{appearance_id}.json"

def mesh_member(appearance_id: str) -> str:
    """! @brief Container member name for one appearance's canonical body mesh."""
    return f"mesh_{appearance_id}.obj"

## Fixed body-description slots a secondary pipeline / LLM action fills. These are
## era-specific (hair greys, physique changes), so they live on an APPEARANCE, not
## the person: one person can have several appearances across their life.
BODY_FIELDS: tuple[str, ...] = (
    "hair_color", "hair_style", "hair_length",
    "skin_tone", "physique", "height", "eye_color",
    "facial_hair", "distinguishing_marks",
)

## Person-level biographical slots, stable across life. birthday/death_date are
## ISO dates (date pickers), aliases/tags are lists, notes is multiline; the rest
## are short free text. relationships is structured separately (see RELATION_LINES).
BIO_FIELDS: tuple[str, ...] = (
    "birthday", "death_date", "gender", "occupation",
    "birthplace", "location", "notes",
)
LIST_FIELDS: tuple[str, ...] = ("aliases", "tags")

## Bio fields whose value is a closed set. Anything outside the set (or its empty
## "unset" value) is rejected so gender can't drift into free text server-side.
BIO_CHOICES: dict[str, tuple[str, ...]] = {"gender": ("", "male", "female")}

## Relationship lines split into single-entry and multi-entry. mother/father/spouse
## hold at most one person (a person has one of each at a time — an ex goes under
## ex_spouses). The rest are lists. Each edge links a known person (uuid) or names
## an external person (uuid None) with no record.
SINGLE_RELATIONS: tuple[str, ...] = ("mother", "father", "spouse")
MULTI_RELATIONS: tuple[str, ...] = (
    "siblings", "children", "ex_spouses",
    "step_parents", "step_siblings", "step_children",
)
RELATION_LINES: tuple[str, ...] = SINGLE_RELATIONS + MULTI_RELATIONS

def persons_dir(media_dir: str) -> str:
    """! @brief Absolute path to the media root's `.persons` store, created on demand."""
    d = os.path.join(media_dir, ".persons")
    os.makedirs(d, exist_ok=True)
    return d

def _path(media_dir: str, person_uuid: str) -> str:
    return os.path.join(persons_dir(media_dir), f"{person_uuid}.person")

def blank_appearance(appearance_id: str) -> dict[str, Any]:
    """! @brief An empty time-scoped appearance (one stable era of a person's look).
    @return A record holding this era's own body fields, identity centroids, image
            membership and date span, so nothing is ever averaged across eras. The
            date span is descriptive only — appearances are formed from embedding
            drift, not dates, so a wrong scan-date can't move a photo between eras.
    """
    return {
        "id": appearance_id,
        "label": "",
        "body": {k: "" for k in BODY_FIELDS},
        "rel_paths": [],
        "centroids": {"arcface": None, "dino": None},
        "date_span": {"min": None, "max": None},
        "has_tpose": False,
        "has_mesh": False,
    }

def _blank(person_uuid: str) -> dict[str, Any]:
    return {
        "uuid": person_uuid,
        "name": "",
        "bio": {k: "" for k in BIO_FIELDS},
        "lists": {k: [] for k in LIST_FIELDS},
        "relationships": {k: [] for k in RELATION_LINES},
        "clusters": {"face": [], "body": []},
        "appearances": [],
    }

def _edge(uuid_or_none: Optional[str], name: str) -> dict[str, Any]:
    """! @brief One relationship endpoint: a link to a known person, or an external name.
    @param uuid_or_none The linked person's uuid, or None for an external person who
           has no record (too few photos in the library).
    @return {uuid, name}; uuid is None for externals so reads can tell them apart.
    """
    return {"uuid": uuid_or_none, "name": name}

def _migrate(desc: dict[str, Any]) -> dict[str, Any]:
    """! @brief Bring an older record up to the current schema without losing data.
    @return The descriptor with any missing bio/list/relationship keys filled in,
            so records written before these fields existed still load cleanly.
    """
    desc.setdefault("bio", {})
    for k in BIO_FIELDS:
        desc["bio"].setdefault(k, "")
    desc.setdefault("lists", {k: [] for k in LIST_FIELDS})
    for k in LIST_FIELDS:
        desc["lists"].setdefault(k, [])
    desc.setdefault("relationships", {k: [] for k in RELATION_LINES})
    for k in RELATION_LINES:
        desc["relationships"].setdefault(k, [])
    desc.setdefault("clusters", {"face": [], "body": []})
    desc.setdefault("appearances", [])
    return desc

def new_uuid() -> str:
    """! @brief Fresh stable id a person record owns for life (survives reclustering)."""
    return uuid.uuid4().hex

def read(media_dir: str, person_uuid: str) -> Optional[dict[str, Any]]:
    """! @brief Load a person's descriptor.
    @return The descriptor dict, or None if the record file is missing/corrupt.
    """
    path = _path(media_dir, person_uuid)
    if not os.path.exists(path):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            return _migrate(json.loads(z.read(DESCRIPTOR)))
    except Exception:
        return None

def read_member(media_dir: str, person_uuid: str, member: str) -> Optional[bytes]:
    """! @brief Read one raw member (e.g. mesh.obj) from a person container.
    @return The bytes, or None when the record or member is absent.
    """
    path = _path(media_dir, person_uuid)
    if not os.path.exists(path):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            return z.read(member)
    except (KeyError, Exception):
        return None

def _rewrite(media_dir: str, person_uuid: str, members: dict[str, bytes]) -> None:
    """! @brief Atomically write a person container from a full member map."""
    path = _path(media_dir, person_uuid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(buf.getvalue())
    os.replace(tmp, path)

def _members(media_dir: str, person_uuid: str) -> dict[str, bytes]:
    path = _path(media_dir, person_uuid)
    if not os.path.exists(path):
        return {}
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}

def write(media_dir: str, descriptor: dict[str, Any]) -> str:
    """! @brief Persist a descriptor, preserving any existing tpose/mesh members.
    @return The person uuid written.
    """
    person_uuid = descriptor["uuid"]
    members = _members(media_dir, person_uuid)
    members[DESCRIPTOR] = json.dumps(descriptor, ensure_ascii=False, indent=1).encode()
    _rewrite(media_dir, person_uuid, members)
    return person_uuid

def put_member(media_dir: str, person_uuid: str, member: str, data: bytes) -> None:
    """! @brief Attach/replace a binary member (an appearance's tpose/mesh) in the container."""
    members = _members(media_dir, person_uuid)
    members[member] = data
    _rewrite(media_dir, person_uuid, members)

def create(media_dir: str, name: str = "") -> dict[str, Any]:
    """! @brief Create and persist a blank person record.
    @return The new descriptor.
    """
    desc = _blank(new_uuid())
    desc["name"] = name
    write(media_dir, desc)
    return desc

def get_appearance(desc: dict[str, Any], appearance_id: str) -> Optional[dict[str, Any]]:
    """! @brief Find an appearance by id within a loaded descriptor."""
    for a in desc["appearances"]:
        if a["id"] == appearance_id:
            return a
    return None

def upsert_appearance(media_dir: str, person_uuid: str,
                      appearance: dict[str, Any]) -> bool:
    """! @brief Insert or replace one appearance (matched by id) and persist.
    @return True on success, False when the person record is missing.
    """
    desc = read(media_dir, person_uuid)
    if desc is None:
        return False
    desc["appearances"] = [a for a in desc["appearances"] if a["id"] != appearance["id"]]
    desc["appearances"].append(appearance)
    write(media_dir, desc)
    return True

def set_field(media_dir: str, person_uuid: str, section: str, key: str,
              value: Any, appearance_id: Optional[str] = None) -> bool:
    """! @brief Set one field in the shared per-field store used by pipeline and actions.
    @param section 'bio' (person-level) or 'body' (era-level, needs appearance_id).
    @param appearance_id Required for body fields; selects which era to write.
    @return True if written; False if the record/appearance is missing or the key is
            not a defined slot (typo keys are rejected so the schema can't drift).
    """
    desc = read(media_dir, person_uuid)
    if desc is None:
        return False
    if section == "root" and key == "name":
        desc["name"] = value
    elif section == "bio" and key in BIO_FIELDS:
        if key in BIO_CHOICES and value not in BIO_CHOICES[key]:
            return False
        desc["bio"][key] = value
    elif section == "body" and key in BODY_FIELDS:
        app = get_appearance(desc, appearance_id or "")
        if app is None:
            return False
        app["body"][key] = value
    elif section == "list" and key in LIST_FIELDS:
        desc["lists"][key] = value if isinstance(value, list) else [value]
    else:
        return False
    write(media_dir, desc)
    return True

def set_relationship(media_dir: str, person_uuid: str, line: str,
                     edges: list) -> bool:
    """! @brief Replace one relationship line (mother/father/spouse/siblings/children).
    @param edges List of {uuid, name}; uuid None marks an external person. The caller
           is responsible for also writing the reciprocal edge on the other person so
           both records hold the link and one corrupt file can't erase the other side.
    @return True on success, False when the record is missing or the line is unknown.
    """
    if line not in RELATION_LINES:
        return False
    desc = read(media_dir, person_uuid)
    if desc is None:
        return False
    # Single-entry lines hold at most one person; keep only the last set.
    desc["relationships"][line] = edges[-1:] if line in SINGLE_RELATIONS else edges
    write(media_dir, desc)
    return True

## Which line becomes which on the other person when an edge is written. Symmetric
## lines map to themselves; parent<->child is asymmetric (resolved by gender below).
## Used to write the reciprocal edge so relationships are stored on BOTH people.
_RECIPROCAL = {
    "spouse": "spouse", "ex_spouses": "ex_spouses",
    "siblings": "siblings", "step_siblings": "step_siblings",
    "mother": "children", "father": "children",
    "step_parents": "step_children",
}

def reciprocal_line(line: str, target_is_female: Optional[bool]) -> Optional[str]:
    """! @brief The line an edge should be written under on the OTHER person.
    @param target_is_female Picks mother/father (for children) or the step equivalent
           (for step_children); None falls back to the father-side line by convention.
    @return The reciprocal line name, or None when it can't be inferred here.
    """
    if line in _RECIPROCAL:
        return _RECIPROCAL[line]
    if line == "children":
        return "mother" if target_is_female else "father"
    if line == "step_children":
        return "step_parents"
    return None

def check_reciprocity(media_dir: str) -> list[dict[str, Any]]:
    """! @brief Find relationship edges present on one person but missing on the other.
    @return One entry per one-sided edge: {person, line, other, other_name}. Reads
            never auto-repair — mismatches surface in the review tab so a corrupt or
            half-written edge is shown, not silently reconciled by overwriting.
    """
    people = {d["uuid"]: d for d in list_all(media_dir)}
    problems = []
    for uid, desc in people.items():
        for line, edges in desc["relationships"].items():
            for e in edges:
                other = e.get("uuid")
                if not other or other not in people:
                    continue
                back = people[other]["relationships"]
                if not any(be.get("uuid") == uid for line2 in back.values() for be in line2):
                    problems.append({"person": uid, "line": line,
                                     "other": other, "other_name": e.get("name", "")})
    return problems

def list_all(media_dir: str) -> list[dict[str, Any]]:
    """! @brief Every valid person descriptor on disk (the source of truth)."""
    out = []
    for fn in os.listdir(persons_dir(media_dir)):
        if fn.endswith(".person"):
            desc = read(media_dir, fn[:-len(".person")])
            if desc is not None:
                out.append(desc)
    return out