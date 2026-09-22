"""common.py: pure helpers (tags, boxes, dates)."""
import pytest
import common


def test_tag_sentinel_roundtrip():
    assert common.make_tag("cat") == "cat"
    assert common.make_tag("cat", confirmed=False) == "?cat"
    assert common.make_tag("?cat", confirmed=False) == "?cat"      # never double prefix
    assert common.make_tag("?cat", confirmed=True) == "cat"
    assert common.tag_name("?cat") == "cat" and common.tag_name("cat") == "cat"
    assert common.tag_is_confirmed("cat") and not common.tag_is_confirmed("?cat")
    assert common.count_unconfirmed_tags(["a", "?b", "?c"]) == 2
    assert common.count_unconfirmed_tags(None) == 0


def test_clamp_box():
    b = common.clamp_box({"cx": 0.9, "cy": 0.5, "w": 0.4, "h": 0.2, "extra": 1})
    assert b["extra"] == 1
    assert b["cx"] + b["w"] / 2 <= 1.0 + 1e-9
    assert abs(b["w"] - 0.3) < 1e-9
    assert common.clamp_box({"cx": 1.5, "cy": 0.5, "w": 0.1, "h": 0.1}) is None   # fully outside
    assert common.clamp_box({"cx": "x"}) is None


def test_iou_center():
    a = {"cx": .5, "cy": .5, "w": .4, "h": .4}
    assert common.iou_center(a, a) == pytest.approx(1.0)
    assert common.iou_center(a, {"cx": .9, "cy": .9, "w": .1, "h": .1}) == 0.0
    half = {"cx": .7, "cy": .5, "w": .4, "h": .4}
    assert common.iou_center(a, half) == pytest.approx(0.2 / 0.6)


@pytest.mark.parametrize("s,end,exp", [
    ("2024", False, "2024-01-01"), ("2024", True, "2024-12-31"),
    ("2024-02", False, "2024-02-01"), ("2024-02", True, "2024-02-29"),
    ("2023/02", True, "2023-02-28"), ("2024-13", False, None),
    ("2024-02-30", False, None), ("2024-2-5", False, "2024-02-05"),
    ("junk", False, None),
])
def test_norm_date_literal(s, end, exp):
    assert common.norm_date_literal(s, end=end) == exp