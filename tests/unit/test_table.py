from pathlib import Path

import numpy as np
import pytest

from luregen.profile.table import StationTable, StationTableError

PRESET = (
    Path(__file__).resolve().parents[2] / "spec/designs/glide/profiles/presets/placeholder_175.csv"
)


def make(**kw) -> StationTable:
    args = dict(
        nose=(0, 0),
        tail=(100, 0),
        x=[10, 50, 90],
        center_z=[0, 1, 0],
        v_radius=[3, 10, 2],
        half_width=[1, 4, 1],
        source="test",
    )
    args.update(kw)
    return StationTable(**args)


def test_round_trip(tmp_path: Path):
    t = make(meta={"note": "hello"})
    p = tmp_path / "s.csv"
    t.to_csv(p)
    back = StationTable.load_csv(p)
    assert back.nose == t.nose and back.tail == t.tail and back.source == "test"
    assert back.meta == {"note": "hello"}
    for col in ("x", "center_z", "v_radius", "half_width"):
        np.testing.assert_allclose(getattr(back, col), getattr(t, col))


@pytest.mark.parametrize(
    "kw",
    [
        dict(x=[10, 10, 90]),  # not strictly increasing
        dict(x=[0, 50, 90]),  # on the nose tip
        dict(v_radius=[3, 0, 2]),  # zero radius
        dict(half_width=[1, 4]),  # length mismatch
        dict(center_z=[0, float("nan"), 0]),
    ],
)
def test_validation_errors(kw):
    with pytest.raises(StationTableError):
        make(**kw)


def test_arrays_are_read_only():
    t = make()
    with pytest.raises(ValueError):
        t.x[0] = 5


def test_scaled_and_properties():
    t = make().scaled(2.0)
    assert t.length == 200
    assert t.max_depth == 40 and t.max_width == 16
    assert t.meta["scaled_by"] == "2"


def test_bad_header_rejected(tmp_path: Path):
    p = tmp_path / "bad.csv"
    p.write_text("x,center_z,v_radius,half_width\n1,0,1,1\n")
    with pytest.raises(StationTableError):
        StationTable.load_csv(p)


def test_placeholder_preset_matches_reference_ratios():
    t = StationTable.load_csv(PRESET)
    assert len(t) == 23 and t.length == 175
    assert t.max_depth / t.length == pytest.approx(0.29, abs=0.005)
    assert t.max_width / t.max_depth == pytest.approx(0.34, abs=0.005)
