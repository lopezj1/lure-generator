"""Heightmaps for 3D CAM and the fast cut simulation (#42, #43).

- `Grid`: a regular XY grid of cell centres, with separate X and Y spacing (finishing aligns
  its rows with the raster lines).
- `raster_top`: the top surface of triangle meshes by z-buffer rasterization (numpy only;
  trimesh's ray tests need embree for speed, which isn't in the pinned dependencies). Only
  upward-facing triangles are drawn, so a closed mesh gives its top surface.
- `tip_surface`: the lowest tool-tip Z over every cell such that the tool doesn't cut below
  the target (a drop-cutter on the heightmap, i.e. a grey dilation): a flat kernel for flat
  end mills, a ball kernel for ball noses, plus an optional shank above the flutes.
- `sweep_surface`: the inverse (a grey erosion) for simulating a set of tool-tip positions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import maximum_filter1d

_CHUNK = 4_000_000  # (triangle, cell) candidate pairs per rasterization chunk


@dataclass(frozen=True)
class Grid:
    x0: float  # min-x edge
    y0: float
    rx: float  # cell size along X
    ry: float  # along Y
    nx: int
    ny: int

    @classmethod
    def cover(cls, bounds, rx: float, ry: float | None = None) -> Grid:
        """A grid whose cells cover (xmin, ymin, xmax, ymax)."""
        ry = ry or rx
        x0, y0, x1, y1 = bounds
        nx, ny = max(1, math.ceil((x1 - x0) / rx)), max(1, math.ceil((y1 - y0) / ry))
        return cls(x0, y0, rx, ry, nx, ny)

    @property
    def xs(self) -> np.ndarray:
        return self.x0 + (np.arange(self.nx) + 0.5) * self.rx

    @property
    def ys(self) -> np.ndarray:
        return self.y0 + (np.arange(self.ny) + 0.5) * self.ry

    @property
    def shape(self) -> tuple[int, int]:
        return self.ny, self.nx

    def index(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        """Nearest cell (i, j) of points (may fall outside the grid)."""
        i = np.floor((np.asarray(x) - self.x0) / self.rx).astype(np.int64)
        j = np.floor((np.asarray(y) - self.y0) / self.ry).astype(np.int64)
        return i, j


def raster_top(vertices: np.ndarray, faces: np.ndarray, grid: Grid, fill: float) -> np.ndarray:
    """Max Z of the upward-facing triangles over each cell centre; `fill` where none.

    Scanline rasterization, vectorized: each (triangle, row) pair gets the X span of the
    triangle on the row's centre line, each covered cell centre the triangle's plane Z.
    """
    z = np.full(grid.shape, -np.inf)
    tri = np.asarray(vertices, float)[np.asarray(faces)]
    if len(tri):
        a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
        nz = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
        tri = tri[nz > 1e-9]
    if len(tri):
        lo, hi = tri[:, :, 1].min(1), tri[:, :, 1].max(1)
        j0 = np.maximum(np.ceil((lo - grid.y0) / grid.ry - 0.5), 0).astype(np.int64)
        j1 = np.minimum(np.floor((hi - grid.y0) / grid.ry - 0.5), grid.ny - 1).astype(np.int64)
        rows = np.maximum(j1 - j0 + 1, 0)
        keep = rows > 0
        tri, j0, rows = tri[keep], j0[keep], rows[keep]
        # plane z = A x + B y + C per triangle
        e1, e2 = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
        n = np.cross(e1, e2)
        A, B = -n[:, 0] / n[:, 2], -n[:, 1] / n[:, 2]
        C = tri[:, 0, 2] - A * tri[:, 0, 0] - B * tri[:, 0, 1]
        zr = np.stack([tri[:, :, 2].min(1), tri[:, :, 2].max(1)])  # clamp steep facets
        cum = np.cumsum(rows)
        start = 0
        while start < len(tri):
            base = cum[start - 1] if start else 0
            stop = max(int(np.searchsorted(cum, base + _CHUNK // 8, side="right")), start + 1)
            sl = slice(start, stop)
            _raster_rows(z, grid, tri[sl], j0[sl], rows[sl], A[sl], B[sl], C[sl], zr[:, sl])
            start = stop
    z[~np.isfinite(z)] = fill
    return z


def _raster_rows(z, grid: Grid, tri, j0, rows, A, B, C, zr) -> None:
    t = np.repeat(np.arange(len(tri)), rows)
    j = j0[t] + np.arange(len(t)) - np.repeat(np.cumsum(rows) - rows, rows)
    y = grid.y0 + (j + 0.5) * grid.ry
    xl = np.full(len(t), np.inf)
    xr = np.full(len(t), -np.inf)
    for k in range(3):
        p, q = tri[t, k], tri[t, (k + 1) % 3]
        ylo, yhi = np.minimum(p[:, 1], q[:, 1]), np.maximum(p[:, 1], q[:, 1])
        ok = (y >= ylo) & (y <= yhi)
        dy = q[:, 1] - p[:, 1]
        flat = np.abs(dy) < 1e-12
        u = np.where(flat, 0.0, (y - p[:, 1]) / np.where(flat, 1.0, dy))
        x = p[:, 0] + u * (q[:, 0] - p[:, 0])
        # a horizontal edge on the row line contributes both its ends
        xa = np.where(flat, np.minimum(p[:, 0], q[:, 0]), x)
        xb = np.where(flat, np.maximum(p[:, 0], q[:, 0]), x)
        xl = np.where(ok, np.minimum(xl, xa), xl)
        xr = np.where(ok, np.maximum(xr, xb), xr)
    i0 = np.maximum(np.ceil((xl - grid.x0) / grid.rx - 0.5 - 1e-9), 0)
    i1 = np.minimum(np.floor((xr - grid.x0) / grid.rx - 0.5 + 1e-9), grid.nx - 1)
    good = np.isfinite(xl) & (i1 >= i0)
    t, j, i0, i1 = t[good], j[good], i0[good].astype(np.int64), i1[good].astype(np.int64)
    w = i1 - i0 + 1
    s = np.repeat(np.arange(len(t)), w)
    i = i0[s] + np.arange(len(s)) - np.repeat(np.cumsum(w) - w, w)
    tt, jj = t[s], j[s]
    zz = A[tt] * (grid.x0 + (i + 0.5) * grid.rx) + B[tt] * (grid.y0 + (jj + 0.5) * grid.ry) + C[tt]
    zz = np.clip(zz, zr[0, tt], zr[1, tt])
    np.maximum.at(z, (jj, i), zz)


# --- morphology ---------------------------------------------------------------------------


def _max_shifted(out: np.ndarray, a: np.ndarray, dy: int, dx: int, drop: float = 0.0) -> None:
    """out[j, i] = max(out[j, i], a[j + dy, i + dx] − drop) where the source is in range."""
    ny, nx = a.shape
    ys, yd = (slice(dy, ny), slice(0, ny - dy)) if dy >= 0 else (slice(0, ny + dy), slice(-dy, ny))
    xs, xd = (slice(dx, nx), slice(0, nx - dx)) if dx >= 0 else (slice(0, nx + dx), slice(-dx, nx))
    view = out[yd, xd]
    src = a[ys, xs]
    np.maximum(view, src - drop if drop else src, out=view)


def dilate_flat(z: np.ndarray, r: float, grid: Grid) -> np.ndarray:
    """max of z over a disc of radius r around each cell (flat end mill drop-cutter)."""
    ky = int(math.floor(r / grid.ry))
    out = np.full_like(z, -np.inf)
    rows: dict[int, np.ndarray] = {}
    for dy in range(-ky, ky + 1):
        half = int(math.floor(math.sqrt(max(r * r - (dy * grid.ry) ** 2, 0.0)) / grid.rx))
        if half not in rows:
            rows[half] = maximum_filter1d(z, 2 * half + 1, axis=1, mode="constant", cval=-np.inf)
        _max_shifted(out, rows[half], dy, 0)
    return out


def dilate_ball(z: np.ndarray, r: float, grid: Grid) -> np.ndarray:
    """max of z(p + d) − (r − √(r² − |d|²)) over |d| ≤ r (ball end mill drop-cutter)."""
    ky, kx = int(math.floor(r / grid.ry)), int(math.floor(r / grid.rx))
    out = np.full_like(z, -np.inf)
    for dy in range(-ky, ky + 1):
        for dx in range(-kx, kx + 1):
            d2 = (dx * grid.rx) ** 2 + (dy * grid.ry) ** 2
            if d2 > r * r:
                continue
            drop = r - math.sqrt(r * r - d2)
            _max_shifted(out, z, dy, dx, drop)
    return out


@dataclass(frozen=True)
class Cutter:
    """Tool geometry for 3D: the cutting end plus an optional wider shank above the flutes."""

    shape: str  # "flat" | "ball"
    diameter: float
    flute_length: float | None = None
    shank_diameter: float | None = None

    @property
    def r(self) -> float:
        return self.diameter / 2


def tip_surface(target: np.ndarray, cutter: Cutter, grid: Grid, allowance: float = 0.0):
    """Lowest tool-tip Z per cell keeping `allowance` (approximately normal) above `target`."""
    # A tool grown by the allowance, then its tip moved back up by the allowance.
    r = cutter.r + allowance
    dilate = dilate_ball if cutter.shape == "ball" else dilate_flat
    tip = dilate(target, r, grid) + allowance
    if cutter.shank_diameter and cutter.flute_length and cutter.shank_diameter > cutter.diameter:
        shank = dilate_flat(target, cutter.shank_diameter / 2 + allowance, grid)
        tip = np.maximum(tip, shank + allowance - cutter.flute_length)
    return tip


def sweep_surface(tipmin: np.ndarray, cutter: Cutter, grid: Grid) -> np.ndarray:
    """Surface cut by a tool whose lowest tip Z per cell is `tipmin` (+inf where unvisited)."""
    neg = -tipmin
    if cutter.shape == "ball":
        return -dilate_ball(neg, cutter.r, grid)
    return -dilate_flat(neg, cutter.r, grid)
