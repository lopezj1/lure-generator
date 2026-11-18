import os
import time

import pytest
from build123d import Box, Location, Pos

from luregen.geometry.validate import (
    GeometryError,
    GuardedCrash,
    assert_valid,
    run_guarded,
    void_count,
)


def hollow_box():
    outer = Box(20, 20, 20)
    return outer - Box(10, 10, 10)  # fully enclosed void: 1 solid, 2 shells


def test_assert_valid_counts():
    box = Box(10, 10, 10)
    assert_valid(box, solids=1, shells=1)
    with pytest.raises(GeometryError, match="expected 2 shell"):
        assert_valid(box, solids=1, shells=2)

    hollow = hollow_box()
    assert void_count(hollow) == 1
    assert_valid(hollow, solids=1, shells=2)
    with pytest.raises(GeometryError, match="expected 1 shell"):
        assert_valid(hollow, solids=1, shells=1)

    two = Box(5, 5, 5) + Pos(20, 0, 0) * Box(5, 5, 5)
    assert void_count(two) == 0
    with pytest.raises(GeometryError, match="expected 1 solid"):
        assert_valid(two, solids=1)
    assert_valid(two, solids=2, shells=2)


def test_assert_valid_rejects_none():
    with pytest.raises(GeometryError):
        assert_valid(None)


def _box_volume(size: float) -> float:
    return Box(size, size, size).volume


def _moved_box():
    return Location((5, 0, 0)) * Box(2, 2, 2)


def _raise():
    raise ValueError("boom")


def _sleep():
    time.sleep(10)


def test_run_guarded_returns_values_and_shapes():
    assert run_guarded(_box_volume, 3.0) == pytest.approx(27.0)
    shape = run_guarded(_moved_box)
    assert shape.volume == pytest.approx(8.0)
    x_center = shape.center().X
    assert x_center == pytest.approx(5.0)


def test_run_guarded_catches_a_crash():
    with pytest.raises(GuardedCrash, match="SIGABRT"):
        run_guarded(os.abort)


def test_run_guarded_reraises_python_errors():
    with pytest.raises(GeometryError, match="ValueError: boom"):
        run_guarded(_raise)


def test_run_guarded_timeout():
    with pytest.raises(GuardedCrash, match="timed out"):
        run_guarded(_sleep, timeout=0.5)
