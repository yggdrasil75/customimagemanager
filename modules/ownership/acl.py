"""! @file
@brief Access control lists for library files and folders.

The model is NFSv4.1's (RFC 8881 section 6) with Windows / NTFS evaluation
order, so an ACL here reads like one on a file share:

  - an ACL is a list of ACEs; each ACE is ALLOW, DENY or AUDIT for one
    principal and an access mask (the RFC's bit values);
  - principals: user:<id>, group:<id>, host:<name> (a host account imported from
    disk, kept but never matched here), OWNER@ (the object's owner; inherited,
    it means the owner of the file it lands on, NTFS "CREATOR OWNER"),
    AUTHENTICATED@ (anyone signed in), ANONYMOUS@ (a visitor with a public
    link) and EVERYONE@ (both);
  - a folder ACE inherits with FILE_INHERIT / DIRECTORY_INHERIT, optionally
    NO_PROPAGATE_INHERIT (immediate children only) and INHERIT_ONLY (not the
    folder itself); NTFS's "Applies to" choices map onto these flags;
  - an object may be protected (NTFS "disable inheritance"): nothing above it
    applies to it;
  - evaluation is NTFS canonical order: explicit DENY, explicit ALLOW, then
    each ancestor's inherited DENY and ALLOW, nearest first. For each
    requested bit the first matching ACE that mentions it decides; a bit no
    ACE grants is denied;
  - the owner can always read and change the ACL (READ_ACL / WRITE_ACL),
    whatever the entries say, as on NTFS.

Above the library root sits an implicit base ACL that the ownership policy
derives from the path (the public library, users/<name>/, partner shares).
It is inherited like any other, so an ACL that adds or denies an entry still
lets everything else through, and a protected folder cuts the base off: only
its own entries apply below it.

Pure functions only: no Flask, no DB. module.py feeds them rows.
"""

# -- access mask (RFC 8881 section 6.2.1.3.1) --------------------------------------
READ_DATA = 0x00000001          # read a file / list a folder
WRITE_DATA = 0x00000002         # change a file's content / add a file to a folder
APPEND_DATA = 0x00000004        # append to a file / add a subfolder
READ_NAMED_ATTRS = 0x00000008   # read tags, regions, description (extended attributes)
WRITE_NAMED_ATTRS = 0x00000010  # change them
EXECUTE = 0x00000020            # traverse a folder (kept for parity; nothing is executed)
DELETE_CHILD = 0x00000040       # delete anything in a folder
READ_ATTRIBUTES = 0x00000080    # read size, dates
WRITE_ATTRIBUTES = 0x00000100   # change dates
DELETE = 0x00010000             # delete / move away the object itself
READ_ACL = 0x00020000           # see the ACL and owner
WRITE_ACL = 0x00040000          # change the ACL
WRITE_OWNER = 0x00080000        # take or give ownership
SYNCHRONIZE = 0x00100000

## @brief (name, bit, label on a file, label on a folder) in the order the UI lists them.
RIGHTS = (
    ("read_data", READ_DATA, "Read file", "List folder"),
    ("write_data", WRITE_DATA, "Write file (new versions)", "Add files"),
    ("append_data", APPEND_DATA, "Append data", "Create subfolders"),
    ("read_named_attrs", READ_NAMED_ATTRS, "Read tags & metadata", "Read tags & metadata"),
    ("write_named_attrs", WRITE_NAMED_ATTRS, "Edit tags & metadata", "Edit tags & metadata"),
    ("execute", EXECUTE, "Execute", "Traverse folder"),
    ("delete_child", DELETE_CHILD, "-", "Delete subfolders and files"),
    ("read_attributes", READ_ATTRIBUTES, "Read attributes", "Read attributes"),
    ("write_attributes", WRITE_ATTRIBUTES, "Write attributes", "Write attributes"),
    ("delete", DELETE, "Delete", "Delete"),
    ("read_acl", READ_ACL, "Read permissions", "Read permissions"),
    ("write_acl", WRITE_ACL, "Change permissions", "Change permissions"),
    ("write_owner", WRITE_OWNER, "Take ownership", "Take ownership"),
    ("synchronize", SYNCHRONIZE, "Synchronize", "Synchronize"),
)
RIGHT_BITS = {name: bit for name, bit, _f, _d in RIGHTS}
ALL = 0
for _n, _b, _f, _d in RIGHTS:
    ALL |= _b

READ = READ_DATA | READ_ATTRIBUTES | READ_NAMED_ATTRS | READ_ACL | SYNCHRONIZE
WRITE = WRITE_DATA | APPEND_DATA | WRITE_ATTRIBUTES | WRITE_NAMED_ATTRS | SYNCHRONIZE
## @brief NTFS's simple permissions, most to least.
PRESETS = {
    "full": ALL,
    "modify": READ | WRITE | EXECUTE | DELETE,
    "read_write": READ | WRITE | EXECUTE,
    "read": READ | EXECUTE,
    "write": WRITE,                    # a drop box: add, but not see
}
PRESET_LABELS = {"full": "Full control", "modify": "Modify", "read_write": "Read & write",
                 "read": "Read", "write": "Write only (drop box)"}

# -- ACE flags (RFC 8881 section 6.2.1.4.1) ------------------------------------------
FILE_INHERIT = 0x01
DIRECTORY_INHERIT = 0x02
NO_PROPAGATE = 0x04
INHERIT_ONLY = 0x08
SUCCESSFUL_ACCESS = 0x10     # AUDIT: log granted requests
FAILED_ACCESS = 0x20         # AUDIT: log refused requests
INHERITED = 0x80             # set on the copies evaluate() reports, never stored

FLAG_BITS = {"file_inherit": FILE_INHERIT, "dir_inherit": DIRECTORY_INHERIT,
             "no_propagate": NO_PROPAGATE, "inherit_only": INHERIT_ONLY,
             "success": SUCCESSFUL_ACCESS, "failure": FAILED_ACCESS}
_STORED_FLAGS = FILE_INHERIT | DIRECTORY_INHERIT | NO_PROPAGATE | INHERIT_ONLY | \
    SUCCESSFUL_ACCESS | FAILED_ACCESS

## @brief NTFS "Applies to" for a folder ACE -> flags.
APPLIES_TO = {
    "this_subfolders_files": FILE_INHERIT | DIRECTORY_INHERIT,
    "this_only": 0,
    "this_subfolders": DIRECTORY_INHERIT,
    "this_files": FILE_INHERIT,
    "subfolders_files": FILE_INHERIT | DIRECTORY_INHERIT | INHERIT_ONLY,
    "subfolders": DIRECTORY_INHERIT | INHERIT_ONLY,
    "files": FILE_INHERIT | INHERIT_ONLY,
}

TYPES = ("allow", "deny", "audit")
SPECIAL = ("OWNER@", "EVERYONE@", "AUTHENTICATED@", "ANONYMOUS@")


def mask_of(v):
    """! @brief An access mask from an int, a preset name or a list of right names / presets.
    @throws ValueError for an unknown name.
    """
    if isinstance(v, bool):
        raise ValueError("mask must be a number, a preset or a list of rights")
    if isinstance(v, int):
        return v & ALL
    if isinstance(v, str):
        v = [v]
    if isinstance(v, (list, tuple)):
        m = 0
        for name in v:
            name = str(name)
            if name in PRESETS:
                m |= PRESETS[name]
            elif name in RIGHT_BITS:
                m |= RIGHT_BITS[name]
            else:
                raise ValueError(f"unknown right {name!r}")
        return m
    raise ValueError("mask must be a number, a preset or a list of rights")


def rights_of(mask):
    """! @brief The right names set in a mask."""
    return [name for name, bit, _f, _d in RIGHTS if mask & bit]


def preset_of(mask):
    """! @brief The preset a mask equals, or None (a custom set)."""
    for name, m in PRESETS.items():
        if mask == m:
            return name
    return None


def flags_of(v):
    if isinstance(v, bool):
        raise ValueError("flags must be a number or a list of names")
    if isinstance(v, int):
        return v & _STORED_FLAGS
    m = 0
    for name in v or ():
        if name not in FLAG_BITS:
            raise ValueError(f"unknown flag {name!r}")
        m |= FLAG_BITS[name]
    return m


def norm_who(who):
    """! @brief A principal in canonical form, or ValueError."""
    w = str(who or "").strip()
    if w.upper() in SPECIAL:
        return w.upper()
    kind, _, ident = w.partition(":")
    if kind == "host" and ident.strip():
        # a host account the app has no account for (imported from disk): kept and
        # written back as it is, never matched by the app itself
        return "host:" + ident.strip()
    if kind in ("user", "group"):
        try:
            return f"{kind}:{int(ident)}"
        except ValueError:
            pass
    raise ValueError(f"unknown principal {who!r}")


def normalize_ace(d, is_dir):
    """! @brief Validate one ACE from the API into the stored form
    {"type", "who", "mask", "flags"}. Accepts "rights" (preset / names) for the mask
    and "applies_to" (folders) for the inheritance flags.
    @throws ValueError with a message for the user.
    """
    if not isinstance(d, dict):
        raise ValueError("an entry must be an object")
    t = str(d.get("type") or "allow").lower()
    if t not in TYPES:
        raise ValueError("type must be allow, deny or audit")
    who = norm_who(d.get("who"))
    mask = mask_of(d["mask"] if "mask" in d else d.get("rights", 0))
    if not mask:
        raise ValueError(f"entry for {who} grants nothing")
    if "flags" in d:
        flags = flags_of(d.get("flags") or 0)
    else:   # a folder entry applies to the folder, its subfolders and files unless told otherwise
        flags = (FILE_INHERIT | DIRECTORY_INHERIT) if is_dir else 0
    if "applies_to" in d:
        at = d.get("applies_to")
        if at not in APPLIES_TO:
            raise ValueError(f"unknown applies_to {at!r}")
        flags = (flags & ~(FILE_INHERIT | DIRECTORY_INHERIT | INHERIT_ONLY)) | APPLIES_TO[at]
    if d.get("only_children"):
        flags |= NO_PROPAGATE
    if not is_dir:
        flags &= ~(FILE_INHERIT | DIRECTORY_INHERIT | NO_PROPAGATE | INHERIT_ONLY)
    if t == "audit":
        if not flags & (SUCCESSFUL_ACCESS | FAILED_ACCESS):
            flags |= SUCCESSFUL_ACCESS | FAILED_ACCESS
    else:
        flags &= ~(SUCCESSFUL_ACCESS | FAILED_ACCESS)
    return {"type": t, "who": who, "mask": mask, "flags": flags}


def applies_to_of(flags):
    """! @brief The NTFS "Applies to" name of a folder ACE's flags."""
    f = flags & (FILE_INHERIT | DIRECTORY_INHERIT | INHERIT_ONLY)
    for name, v in APPLIES_TO.items():
        if v == f:
            return name
    return "this_only"


def describe(ace):
    """! @brief An ACE for the UI: the stored fields plus rights / preset / applies_to."""
    d = dict(ace)
    d["rights"] = rights_of(ace["mask"])
    d["preset"] = preset_of(ace["mask"])
    d["applies_to"] = applies_to_of(ace["flags"])
    d["only_children"] = bool(ace["flags"] & NO_PROPAGATE)
    d["inherited"] = bool(ace["flags"] & INHERITED)
    return d


def signature(protected, aces):
    """! @brief A stable key for (protected, entries): equal ACLs share one decision."""
    parts = [("p" if protected else "i")]
    for a in aces:
        parts.append(f"{a['type'][0]}{a['who']}#{a['mask']:x}#{a['flags']:x}")
    return "|".join(parts)


def governs(protected, aces):
    """! @brief Whether an ACL changes access (audit entries alone do not)."""
    return bool(protected) or any(a["type"] in ("allow", "deny") for a in aces)


# -- evaluation ----------------------------------------------------------------------
class Subject:
    """! @brief Who is asking: a signed-in account, or an anonymous link visitor."""

    def __init__(self, user_id=None, group_ids=(), authenticated=True):
        self.user_id = user_id
        self.group_ids = frozenset(group_ids or ())
        self.authenticated = bool(authenticated)

    def matches(self, who, owner_id):
        if who == "EVERYONE@":
            return True
        if who == "AUTHENTICATED@":
            return self.authenticated
        if who == "ANONYMOUS@":
            return not self.authenticated
        if who == "OWNER@":
            return self.authenticated and owner_id is not None and owner_id == self.user_id
        kind, _, ident = who.partition(":")
        try:
            ident = int(ident)
        except ValueError:
            return False
        if kind == "user":
            return self.authenticated and ident == self.user_id
        if kind == "group":
            return self.authenticated and ident in self.group_ids
        return False


def parent_of(path):
    """! @brief The folder holding `path` ("" for the library root; None above it)."""
    path = str(path or "").strip("/")
    if not path:
        return None
    return path.rsplit("/", 1)[0] if "/" in path else ""


def ancestors(path):
    """! @brief The folders above `path`, nearest first, ending with "" (the root)."""
    out = []
    p = parent_of(path)
    while p is not None:
        out.append(p)
        p = parent_of(p)
    return out


def _inherits(ace, distance, target_is_dir):
    """! @brief Does an ancestor's ACE at `distance` (1 = parent) reach the target?"""
    f = ace["flags"]
    if distance > 1 and f & NO_PROPAGATE:
        return False
    return bool(f & (DIRECTORY_INHERIT if target_is_dir else FILE_INHERIT))


def applicable(path, is_dir, explicit, folder_acl, base):
    """! @brief The ACEs that apply to an object, in evaluation order.
    @param explicit    (protected, aces) of the object itself, or None.
    @param folder_acl  fn(folder) -> (protected, aces) or None, for its ancestors.
    @param base        the implicit base ACL (list of ACEs inheriting to everything).
    @return (ordered list of ACE dicts, each with "source": path or None for the base
            and INHERITED set on inherited ones; base_reached: no protection cut it off).
    """
    levels = []
    if explicit is not None:
        prot, aces = explicit
        levels.append([dict(a, source=path) for a in aces if not a["flags"] & INHERIT_ONLY])
        cut = bool(prot)
    else:
        cut = False
    if not cut:
        for dist, anc in enumerate(ancestors(path), start=1):
            acl = folder_acl(anc)
            if acl is None:
                continue
            prot, aces = acl
            levels.append([dict(a, flags=a["flags"] | INHERITED, source=anc)
                           for a in aces if _inherits(a, dist, is_dir)])
            if prot:
                cut = True
                break
    if not cut:
        levels.append([dict(a, flags=a["flags"] | INHERITED, source=None) for a in base])
    ordered = []
    for lv in levels:
        ordered += [a for a in lv if a["type"] == "deny"]
        ordered += [a for a in lv if a["type"] == "allow"]
        ordered += [a for a in lv if a["type"] == "audit"]
    return ordered, not cut


def evaluate(aces, subject, owner_id, want):
    """! @brief Walk the ordered ACEs for the requested bits.
    @return (granted, denied): bits an ALLOW / a DENY decided. Bits in neither are
            undecided (and therefore not granted).
    """
    remaining, granted, denied = want, 0, 0
    for a in aces:
        if a["type"] == "audit" or not remaining:
            continue
        bits = a["mask"] & remaining
        if not bits or not subject.matches(a["who"], owner_id):
            continue
        if a["type"] == "allow":
            granted |= bits
        else:
            denied |= bits
        remaining &= ~bits
    return granted, denied


def effective(aces, subject, owner_id):
    """! @brief The full mask a subject holds (owner extras included)."""
    granted, _d = evaluate(aces, subject, owner_id, ALL)
    if subject.authenticated and owner_id is not None and subject.user_id == owner_id:
        granted |= READ_ACL | WRITE_ACL
    return granted


def audits(aces, subject, owner_id, want, ok):
    """! @brief The AUDIT entries that ask for this request to be logged."""
    flag = SUCCESSFUL_ACCESS if ok else FAILED_ACCESS
    return [a for a in aces if a["type"] == "audit" and a["flags"] & flag
            and a["mask"] & want and subject.matches(a["who"], owner_id)]
