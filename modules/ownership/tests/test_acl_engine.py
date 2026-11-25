"""! @file
@brief The ACL engine on its own (acl.py): masks, inheritance, canonical order."""
import pytest

from modules.ownership import acl

FI, DI, NP, IO = acl.FILE_INHERIT, acl.DIRECTORY_INHERIT, acl.NO_PROPAGATE, acl.INHERIT_ONLY
ALICE = acl.Subject(1, [10])
BOB = acl.Subject(2, [])
ANON = acl.Subject(authenticated=False)


def ace(t, who, mask, flags=FI | DI):
    return {"type": t, "who": who, "mask": acl.mask_of(mask), "flags": flags}


def decide(path, subj, want, dirs, own=None, base=(), owner=None, is_dir=False):
    aces, reached = acl.applicable(path, is_dir, own, dirs.get, list(base))
    granted, _denied = acl.evaluate(aces, subj, owner, want)
    return (granted & want) == want, reached


def test_masks_and_presets():
    assert acl.mask_of("read") == acl.PRESETS["read"]
    assert acl.mask_of(["read_data", "delete"]) == acl.READ_DATA | acl.DELETE
    assert acl.preset_of(acl.ALL) == "full"
    assert acl.PRESETS["modify"] & acl.DELETE and not acl.PRESETS["modify"] & acl.WRITE_ACL
    with pytest.raises(ValueError):
        acl.mask_of(["fly"])
    with pytest.raises(ValueError):
        acl.norm_who("nobody")
    assert acl.norm_who("everyone@") == "EVERYONE@" and acl.norm_who("group:7") == "group:7"


def test_normalize_applies_to_and_files_drop_inheritance():
    a = acl.normalize_ace({"type": "allow", "who": "group:10", "rights": "modify", "applies_to": "files"}, True)
    assert a["flags"] == FI | IO
    f = acl.normalize_ace({"who": "user:1", "rights": "read", "flags": ["file_inherit"]}, False)
    assert f["flags"] == 0
    au = acl.normalize_ace({"type": "audit", "who": "EVERYONE@", "rights": "read"}, False)
    assert au["flags"] & acl.SUCCESSFUL_ACCESS and au["flags"] & acl.FAILED_ACCESS
    with pytest.raises(ValueError):
        acl.normalize_ace({"who": "user:1", "mask": 0}, False)


def test_inheritance_protection_and_base():
    base = [ace("allow", "AUTHENTICATED@", "modify")]
    dirs = {"proj": (True, [ace("allow", "group:10", "modify")])}
    assert decide("proj/a/b.txt", ALICE, acl.READ_DATA, dirs, base=base) == (True, False)
    assert decide("proj/a/b.txt", BOB, acl.READ_DATA, dirs, base=base) == (False, False)   # base cut off
    assert decide("other/b.txt", BOB, acl.READ_DATA, dirs, base=base) == (True, True)
    # a deny without protection leaves everyone else on the base
    dirs = {"pub": (False, [ace("deny", "user:2", "read")])}
    assert decide("pub/x", BOB, acl.READ_DATA, dirs, base=base)[0] is False
    assert decide("pub/x", ALICE, acl.READ_DATA, dirs, base=base)[0] is True


def test_canonical_order_explicit_beats_inherited():
    dirs = {"d": (False, [ace("deny", "group:10", "read")])}
    own = (False, [ace("allow", "user:1", "read", 0)])
    assert decide("d/f", ALICE, acl.READ_DATA, dirs, own=own)[0] is True       # explicit allow first
    dirs = {"d": (False, [ace("allow", "group:10", "read"), ace("deny", "user:1", "read")])}
    assert decide("d/f", ALICE, acl.READ_DATA, dirs)[0] is False                # deny before allow


def test_flags_no_propagate_inherit_only_and_dirs():
    dirs = {"d": (True, [ace("allow", "user:1", "read", FI | NP)])}
    assert decide("d/f", ALICE, acl.READ_DATA, dirs)[0] is True
    assert decide("d/s/f", ALICE, acl.READ_DATA, dirs)[0] is False             # only direct children
    dirs = {"d": (True, [ace("allow", "user:1", "read", FI)])}
    assert decide("d/s", ALICE, acl.READ_DATA, dirs, is_dir=True)[0] is False  # files only, not the subfolder
    assert decide("d/s/f", ALICE, acl.READ_DATA, dirs)[0] is True              # ...but deeper files
    own = (True, [ace("allow", "user:1", "read", FI | IO)])
    assert decide("d", ALICE, acl.READ_DATA, {}, own=own, is_dir=True)[0] is False   # inherit-only


def test_owner_and_special_principals():
    dirs = {"drop": (True, [ace("allow", "AUTHENTICATED@", "write"), ace("allow", "OWNER@", "full")])}
    assert decide("drop/f", BOB, acl.READ_DATA, dirs, owner=1)[0] is False     # drop box: can't look
    assert decide("drop/f", ALICE, acl.READ_DATA, dirs, owner=1)[0] is True    # creator owner can
    assert decide("drop/f", BOB, acl.WRITE_DATA, dirs, owner=1)[0] is True
    dirs = {"pub": (False, [ace("allow", "ANONYMOUS@", "read")])}
    assert decide("pub/f", ANON, acl.READ_DATA, dirs)[0] is True
    assert decide("pub/f", ALICE, acl.READ_DATA, dirs)[0] is False
    assert acl.effective([], ALICE, 1) & acl.WRITE_ACL                         # an owner always manages
    hits = acl.audits([ace("audit", "EVERYONE@", "read", acl.FAILED_ACCESS)], BOB, None, acl.READ_DATA, False)
    assert len(hits) == 1
    assert not acl.audits([ace("audit", "EVERYONE@", "read", acl.FAILED_ACCESS)], BOB, None, acl.READ_DATA, True)