"""wait_for_space blocks while the disk is under its floor and resumes/cancels."""
import shutil
from collections import namedtuple
import common

_DU = namedtuple("usage", "total used free")


def test_floor_scales_with_disk_size(monkeypatch):
    monkeypatch.delenv("CIM_MIN_FREE_GB", raising=False)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _DU(2 << 40, 0, 2 << 40))
    assert common.min_free_bytes(".") == 10 << 30
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _DU(500 << 30, 0, 500 << 30))
    assert common.min_free_bytes(".") == 1 << 30
    monkeypatch.setenv("CIM_MIN_FREE_GB", "0.5")
    assert common.min_free_bytes(".") == 1 << 29


def test_wait_pauses_then_resumes(monkeypatch):
    monkeypatch.setenv("CIM_MIN_FREE_GB", "1")
    free = [100 << 20, 100 << 20, 5 << 30]          # low, low, ok
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _DU(500 << 30, 0, free.pop(0)))
    slept = []
    import time
    monkeypatch.setattr(time, "sleep", slept.append)
    assert common.wait_for_space(".", poll=1) is True
    assert slept == [1, 1]


def test_wait_honours_stop(monkeypatch):
    monkeypatch.delenv("CIM_MIN_FREE_GB", raising=False)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _DU(500 << 30, 0, 0))
    assert common.wait_for_space(".", stop=lambda: True) is False