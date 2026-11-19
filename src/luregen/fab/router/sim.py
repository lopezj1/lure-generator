"""Heightmap cut simulation: stamp flat or ball tools along every move into a stock grid.

The stock is a top-surface heightmap on a regular XY grid (cell centers). Each motion is
sampled at ≤ half a cell and, at every sample, the tool's lower surface lowers the cells
under it: a flat end mill cuts a disc at the tip Z, a ball end mill a hemisphere. Arcs are
linearized first. The result can be diffed against a target heightmap on the same grid
(`compare`) and rendered as a gouge/leftover PNG (`save_png`).
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from luregen.fab.gcode import Comment, Motion, ToolChange, arc_points, interpret, parse
from luregen.fab.router.verify import TOOL_MILL


@dataclass(frozen=True)
class ToolShape:
    shape: Literal["flat", "ball"]
    diameter: float

    @property
    def radius(self) -> float:
        return self.diameter / 2


@dataclass
class Heightmap:
    z: np.ndarray  # (ny, nx), top surface height per cell
    x0: float  # min-x edge of the grid
    y0: float
    res: float

    @classmethod
    def block(cls, lo: tuple[float, float], hi: tuple[float, float], top: float, res: float):
        nx = max(1, round((hi[0] - lo[0]) / res))
        ny = max(1, round((hi[1] - lo[1]) / res))
        return cls(np.full((ny, nx), float(top)), lo[0], lo[1], res)

    def like(self, z: np.ndarray) -> "Heightmap":
        return Heightmap(z, self.x0, self.y0, self.res)

    @property
    def xs(self) -> np.ndarray:
        return self.x0 + (np.arange(self.z.shape[1]) + 0.5) * self.res

    @property
    def ys(self) -> np.ndarray:
        return self.y0 + (np.arange(self.z.shape[0]) + 0.5) * self.res

    def stamp(self, x: float, y: float, z: float, tool: ToolShape) -> None:
        """Lower the surface under a tool whose tip is at (x, y, z)."""
        r = tool.radius
        i0 = max(0, math.floor((x - r - self.x0) / self.res))
        i1 = min(self.z.shape[1], math.ceil((x + r - self.x0) / self.res) + 1)
        j0 = max(0, math.floor((y - r - self.y0) / self.res))
        j1 = min(self.z.shape[0], math.ceil((y + r - self.y0) / self.res) + 1)
        if i0 >= i1 or j0 >= j1:
            return
        dx = self.x0 + (np.arange(i0, i1) + 0.5) * self.res - x
        dy = self.y0 + (np.arange(j0, j1) + 0.5) * self.res - y
        d2 = dy[:, None] ** 2 + dx[None, :] ** 2
        inside = d2 <= r * r
        if tool.shape == "flat":
            surface = np.where(inside, z, np.inf)
        else:
            surface = np.where(inside, z + r - np.sqrt(np.maximum(r * r - d2, 0.0)), np.inf)
        view = self.z[j0:j1, i0:i1]
        np.minimum(view, surface, out=view)

    def cut_segment(self, p0, p1, tool: ToolShape) -> None:
        length = math.dist(p0[:2], p1[:2])
        n = max(1, math.ceil(length / (self.res / 2)))
        if p0[2] == p1[2] and length == 0:
            n = 0
        for k in range(n + 1):
            t = k / n if n else 1.0
            self.stamp(*(a + (b - a) * t for a, b in zip(p0, p1, strict=True)), tool)


def tool_from_comment(text: str) -> ToolShape | None:
    """`(TOOL/MILL,dia,corner_r,…)`: a corner radius of half the diameter means a ball."""
    m = TOOL_MILL.match(text)
    if not m:
        return None
    dia, corner = float(m[1]), float(m[2])
    return ToolShape("ball" if abs(corner - dia / 2) < 0.02 else "flat", dia)


def simulate(
    text: str,
    stock: Heightmap,
    tools: Mapping[int, ToolShape] | None = None,
    default_tool: ToolShape | None = None,
) -> Heightmap:
    """Cut `stock` (in place) with every move of `text`; return it.

    A tool's shape comes from `tools[T]`, else from the last `(TOOL/MILL,…)` comment before
    its `M6`, else `default_tool`.
    """
    tools = dict(tools or {})
    pending: ToolShape | None = None
    for ev in interpret(parse(text)):
        if isinstance(ev, Comment):
            pending = tool_from_comment(ev.text) or pending
        elif isinstance(ev, ToolChange):
            if ev.tool is not None and ev.tool not in tools and pending is not None:
                tools[ev.tool] = pending
        elif isinstance(ev, Motion) and not ev.machine:
            tool = tools.get(ev.tool) if ev.tool is not None else None
            tool = tool or default_tool
            if tool is None or not all(map(math.isfinite, (*ev.start, *ev.end))):
                continue
            pts = [ev.start, *(arc_points(ev, stock.res / 4) if ev.is_arc else [ev.end])]
            for p0, p1 in zip(pts, pts[1:], strict=False):
                stock.cut_segment(p0, p1, tool)
    return stock


def simulate_fast(
    texts: list[str] | str,
    stock: Heightmap,
    tools: Mapping[int, ToolShape] | None = None,
    default_tool: ToolShape | None = None,
) -> Heightmap:
    """Same result as `simulate` (to the grid), vectorized for long 3D programs.

    Every move is sampled at ≤ half a cell; per tool, the lowest tip Z lands in its cell,
    then a grey erosion with the tool's kernel (flat disc or ball) gives the cut surface.
    Several files (one setup split by tool) cut the same stock in order.
    """
    from luregen.fab.router.cam.heightmap import Cutter, Grid, sweep_surface
    from luregen.fab.router.cam.ops3d import tipmin_grid

    grid = Grid(stock.x0, stock.y0, stock.res, stock.res, stock.z.shape[1], stock.z.shape[0])
    paths: dict[ToolShape, list[np.ndarray]] = {}
    for text in [texts] if isinstance(texts, str) else texts:
        known = dict(tools or {})
        pending: ToolShape | None = None
        cur: list = []
        cur_tool: ToolShape | None = None

        def flush(cur: list, cur_tool: ToolShape | None) -> None:
            if cur_tool is not None and len(cur) > 1:
                paths.setdefault(cur_tool, []).append(np.asarray(cur, float))

        for ev in interpret(parse(text)):
            if isinstance(ev, Comment):
                pending = tool_from_comment(ev.text) or pending
            elif isinstance(ev, ToolChange):
                if ev.tool is not None and ev.tool not in known and pending is not None:
                    known[ev.tool] = pending
            elif isinstance(ev, Motion) and not ev.machine:
                tool = (known.get(ev.tool) if ev.tool is not None else None) or default_tool
                if tool is None or not all(map(math.isfinite, (*ev.start, *ev.end))):
                    continue
                if tool != cur_tool or not cur or cur[-1] != ev.start:
                    flush(cur, cur_tool)
                    cur, cur_tool = [ev.start], tool
                cur += arc_points(ev, stock.res / 4) if ev.is_arc else [ev.end]
        flush(cur, cur_tool)
    for tool, pls in paths.items():
        tip = tipmin_grid(pls, grid)
        cut = sweep_surface(tip, Cutter(tool.shape, tool.diameter), grid)
        np.minimum(stock.z, cut, out=stock.z)
    return stock


@dataclass(frozen=True)
class Comparison:
    delta: np.ndarray  # simulated − target; < 0 gouge, > 0 leftover
    tol: float
    gouge_max: float
    leftover_max: float
    gouge_frac: float
    leftover_frac: float

    @property
    def ok(self) -> bool:
        return self.gouge_max <= self.tol and self.leftover_max <= self.tol


def compare(sim: Heightmap, target: Heightmap, tol: float = 0.05) -> Comparison:
    if sim.z.shape != target.z.shape:
        raise ValueError("sim and target heightmaps must share a grid")
    delta = sim.z - target.z
    return Comparison(
        delta=delta,
        tol=tol,
        gouge_max=float(max(0.0, -delta.min())),
        leftover_max=float(max(0.0, delta.max())),
        gouge_frac=float((delta < -tol).mean()),
        leftover_frac=float((delta > tol).mean()),
    )


# Diverging pair with a neutral midpoint: red = gouge (too deep), blue = leftover.
_GOUGE, _NEUTRAL, _LEFTOVER = "#c8402f", "#f0efec", "#2f6db5"


def save_png(path: Path, sim: Heightmap, result: Comparison, title: str = "") -> Path:
    """Two panels: the simulated surface and the deviation from the target."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("dev", [_GOUGE, _NEUTRAL, _LEFTOVER])
    extent = (
        sim.x0,
        sim.x0 + sim.z.shape[1] * sim.res,
        sim.y0,
        sim.y0 + sim.z.shape[0] * sim.res,
    )
    lim = max(abs(float(result.delta.min())), abs(float(result.delta.max())), result.tol * 2)
    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(11, 4.5), constrained_layout=True)
    im0 = ax0.imshow(sim.z, origin="lower", extent=extent, cmap="Greys_r")
    ax0.set_title("Simulated surface Z (mm)", fontsize=10)
    fig.colorbar(im0, ax=ax0, shrink=0.8)
    im1 = ax1.imshow(result.delta, origin="lower", extent=extent, cmap=cmap, vmin=-lim, vmax=lim)
    ax1.set_title(
        f"Sim − target (mm)\nred gouge max {result.gouge_max:.3f}, "
        f"blue leftover max {result.leftover_max:.3f}",
        fontsize=10,
    )
    fig.colorbar(im1, ax=ax1, shrink=0.8)
    for ax in (ax0, ax1):
        ax.set_xlabel("X (mm)")
        ax.set_ylabel("Y (mm)")
        ax.tick_params(labelsize=8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    if title:
        fig.suptitle(title, fontsize=11)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
