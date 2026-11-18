"""Spike #58: the three test shapes, built with the F2 code."""

from pathlib import Path

import numpy as np

from luregen.geometry.envelope import build_envelope, loft3d
from luregen.geometry.validate import assert_valid
from luregen.profile.table import StationTable

ROOT = Path(__file__).resolve().parents[2]
PRESET = ROOT / "spec/designs/glide/profiles/presets/placeholder_175.csv"


def table() -> StationTable:
    return StationTable.load_csv(PRESET)


def loft():
    return build_envelope(table(), "loft3d", "ellipse").solid


def slab():
    return build_envelope(table(), "profile25d", roundover_r=4.0).solid


def inset_table(t: StationTable, x0=0.15, x1=0.8, wall_v=4.0, wall_w=2.5) -> StationTable:
    """Inner cavity stations: mid stations only, radii reduced by a wall (no offset_3d)."""
    m = (t.x > x0 * t.length) & (t.x < x1 * t.length)
    idx = np.flatnonzero(m)
    lo, hi = idx[0] - 1, idx[-1] + 1  # cavity tips sit on the neighbouring stations' axis
    return StationTable(
        nose=(t.x[lo], t.center_z[lo]),
        tail=(t.x[hi], t.center_z[hi]),
        x=t.x[m],
        center_z=t.center_z[m],
        v_radius=t.v_radius[m] - wall_v,
        half_width=t.half_width[m] - wall_w,
        source="spike inset",
    )


def cavity_body():
    t = table()
    outer = loft3d(t, "ellipse")
    inner = loft3d(inset_table(t), "ellipse")
    body = outer - inner
    body = body.solids()[0] if hasattr(body, "solids") and len(body.solids()) == 1 else body
    assert_valid(body, solids=1, shells=2, what="loft minus inset")
    return body, inner


SHAPES = {"loft3d": loft, "profile25d": slab, "cavity": lambda: cavity_body()[0]}
