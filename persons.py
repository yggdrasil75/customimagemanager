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

## Fixed body-description slots a secondary pipeline / LLM action fills per
## person. Kept as a tuple so callers can offer them as form fields and the
## Fixed body-description slots a secondary pipeline / LLM action fills. These are
## era-specific (hair greys, physique changes), so they live on an APPEARANCE, not
## the person: one person can have several appearances across their life.
BODY_FIELDS: tuple[str, ...] = (
    "hair_color", "hair_style", "hair_length",
    "skin_tone", "physique", "height", "eye_color",
    "facial_hair", "distinguishing_marks",
)

## Off-image biographical slots. Free-form; never inferable from one photo.
BIO_FIELDS: tuple[str, ...] = ("birthday", "relationships", "notes")


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
        "clusters": {"face": [], "body": []},
        "appearances": [],
    }


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
            return json.loads(z.read(DESCRIPTOR))
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
        desc["bio"][key] = value
    elif section == "body" and key in BODY_FIELDS:
        app = get_appearance(desc, appearance_id or "")
        if app is None:
            return False
        app["body"][key] = value
    else:
        return False
    write(media_dir, desc)
    return True


def list_all(media_dir: str) -> list[dict[str, Any]]:
    """! @brief Every valid person descriptor on disk (the source of truth)."""
    out = []
    for fn in os.listdir(persons_dir(media_dir)):
        if fn.endswith(".person"):
            desc = read(media_dir, fn[:-len(".person")])
            if desc is not None:
                out.append(desc)
    return out