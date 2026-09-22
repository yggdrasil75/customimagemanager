"""features.py: permission levels and role resolution."""
import features as f


def test_level_of():
    assert f.level_of("write") == f.WRITE and f.level_of("read") == f.READ
    assert f.level_of("block") == f.BLOCK
    assert f.level_of("garbage", fallback=f.READ) == f.READ
    assert f.level_of(f.WRITE) == f.WRITE


def test_admin_has_everything():
    perms = f.effective_permissions("admin", {})
    assert perms and all(v == f.WRITE for v in perms.values())


def test_override_and_has_level():
    key = next(iter(f.registered_keys()))
    perms = f.effective_permissions("viewer", {key: "block"})
    assert not f.has_level(perms, key, f.READ)
    perms = f.effective_permissions("viewer", {key: "write"})
    assert f.has_level(perms, key, f.WRITE)
    assert not f.has_level({}, "nope", f.READ)                   # missing key == block


def test_group_override_below_user_override():
    key = next(iter(f.registered_keys()))
    perms = f.effective_permissions("viewer", {key: "write"}, {key: "block"})
    assert f.has_level(perms, key, f.WRITE)                      # user override wins


def test_catalog_shape():
    cat = f.catalog()
    assert isinstance(cat, (list, dict)) and cat