"""! @file persons.py
@brief Unified, reusable person model that outlives the source images.

A person record is a source-of-truth file under `<media>/.persons/<uuid>.person`,
a zip container holding three members:

  descriptor.json : uuid, name, static body-description fields (hair/skin/
                    physique...), plus off-image facts (birthday, relationships)
                    and the identity centroids (arcface/dino) so the person is
                    still identifiable after every source photo is gone.
  tpose.json      : estimated canonical T-pose keypoints (optional).
  mesh.obj        : SMPLest-X body mesh (optional; added when that lands).

The DB `persons` table is a disposable cache mapping cluster_id -> uuid; it is
rebuilt by scanning `.persons/`, so a lost DB costs only recompute, never the
descriptor/mesh/relationships that live nowhere else.

The static body-description fields are the fixed slots a secondary pipeline (or
an LLM action) fills. They live here so the pipeline and the actions write the
SAME structure through the SAME store — that is the unification.
"""

import io
import json
import os
import uuid
import zipfile
from typing import Any, Optional

DESCRIPTOR = "descriptor.json"
TPOSE = "tpose.json"
MESH = "mesh.obj"

## Fixed body-description slots a secondary pipeline / LLM action fills per
## person. Kept as a tuple so callers can offer them as form fields and the
## store can reject typo keys instead of silently growing the schema.
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


def _blank(person_uuid: str) -> dict[str, Any]:
    return {
        "uuid": person_uuid,
        "name": "",
        "body": {k: "" for k in BODY_FIELDS},
        "bio": {k: "" for k in BIO_FIELDS},
        "clusters": {"face": [], "body": []},
        "centroids": {"arcface": None, "dino": None},
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
    """! @brief Attach/replace a binary member (tpose.json, mesh.obj) in the container."""
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


def set_field(media_dir: str, person_uuid: str, section: str, key: str,
              value: Any) -> bool:
    """! @brief Set one field in the shared per-field store used by pipeline and actions.
    @param section 'body', 'bio', or 'root' (name/uuid live at root).
    @return True if written; False if the record is missing or the key is not a
            defined slot (typo keys are rejected so the schema can't drift).
    """
    desc = read(media_dir, person_uuid)
    if desc is None:
        return False
    if section == "root" and key in ("name",):
        desc[key] = value
    elif section == "body" and key in BODY_FIELDS:
        desc["body"][key] = value
    elif section == "bio" and key in BIO_FIELDS:
        desc["bio"][key] = value
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