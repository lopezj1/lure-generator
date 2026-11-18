"""Station table generation: rails + a width source → `StationTable`.

Stations use cosine spacing, x_i = L/2 · (1 − cos(π i / (N + 1))) for i = 1..N, which
clusters them toward the nose and tail where curvature is highest and keeps the tips out
of the table (they are closed by vertices downstream).
"""

from collections.abc import Callable

import numpy as np

from luregen.profile.extract import ProfileError, Rails
from luregen.profile.table import StationTable

HalfWidthFn = Callable[[np.ndarray, np.ndarray], np.ndarray]  # (x, v_radius) → half_width


def cosine_stations(length: float, n: int) -> np.ndarray:
    """N interior station positions in (0, length), clustered toward both tips."""
    if n < 2:
        raise ProfileError("need at least 2 stations")
    theta = np.pi * np.arange(1, n + 1) / (n + 1)
    return 0.5 * length * (1.0 - np.cos(theta))


def width_from_ratio(ratio: float) -> HalfWidthFn:
    """Config `width_rule` ratio_of_depth: half_width = ratio · v_radius (width/depth)."""
    if ratio <= 0:
        raise ProfileError("width ratio must be > 0")
    return lambda _x, v_radius: ratio * v_radius


def width_from_top(top: Rails, side_length: float) -> HalfWidthFn:
    """Half widths from top-view rails, mapped onto the side view by fraction of body length.

    The top view's own scale is kept for widths; only its x is stretched to the side length,
    so a small length mismatch between the two photos doesn't shift the widest point.
    """

    def half_width(x: np.ndarray, _v_radius: np.ndarray) -> np.ndarray:
        xt = top.nose[0] + np.asarray(x) / side_length * top.length
        left, right = top.at(xt)
        return 0.5 * (left - right)

    return half_width


def build_table(
    side: Rails,
    n_stations: int,
    half_width: HalfWidthFn,
    *,
    source: str = "",
    meta: dict[str, str] | None = None,
) -> StationTable:
    """Sample the side-view rails at cosine-spaced stations and attach half widths."""
    if abs(side.nose[0]) > 1e-9 or abs(side.nose[1]) > 1e-9:
        raise ProfileError("side rails must be in the body frame (nose tip at the origin)")
    x = cosine_stations(side.length, n_stations)
    top, bottom = side.at(x)
    v_radius = 0.5 * (top - bottom)
    hw = np.asarray(half_width(x, v_radius), dtype=float)
    if np.any(v_radius <= 0) or np.any(hw <= 0):
        raise ProfileError("a station has zero depth or width; check the silhouette near the tips")
    return StationTable(
        nose=side.nose,
        tail=side.tail,
        x=x,
        center_z=0.5 * (top + bottom),
        v_radius=v_radius,
        half_width=hw,
        source=source,
        meta=meta or {},
    )
