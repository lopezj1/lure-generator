"""3D outer-face CAM (#42) and the setup-2 output (#43) on a synthetic ellipsoid section.

Regenerate the goldens: UPDATE_GOLDEN=1 pytest tests/unit/test_cnc_3d.py
"""

import math
import os
import re
from dataclasses import replace

import numpy as np
import pytest
import trimesh
from router_jobs import GOLDEN, JOBS, ROUTER, shapeoko

from luregen.fab.router import feeds as fd
from luregen.fab.router.cam import ops3d
from luregen.fab.router.cam import setups as st
from luregen.fab.router.cam.heightmap import (
    Cutter,
    Grid,
    dilate_ball,
    dilate_flat,
    raster_top,
    sweep_surface,
    tapered_ball_height,
    tip_surface,
)
from luregen.fab.router.cam.job import setup_sheet, write_programs
from luregen.fab.router.cam.ops2d import Cut
from luregen.fab.router.post import post_program
from luregen.fab.router.sim import Heightmap, simulate, simulate_fast
from luregen.geometry import cnc_halves as ch

# --- heightmap primitives ---------------------------------------------------------------


def test_raster_top_of_a_tilted_triangle():
    v = np.array([[0.0, 0.0, 1.0], [10.0, 0.0, 2.0], [0.0, 10.0, 3.0]])
    g = Grid.cover((0, 0, 10, 10), 0.5)
    z = raster_top(v, np.array([[0, 1, 2]]), g, fill=-5.0)
    xs, ys = np.meshgrid(g.xs, g.ys)
    inside = xs + ys <= 10
    assert np.allclose(z[inside], 1 + 0.1 * xs[inside] + 0.2 * ys[inside])
    assert np.all(z[~inside] == -5.0)
    # a downward-facing copy is ignored (top surface only)
    z2 = raster_top(v, np.array([[0, 2, 1]]), g, fill=-5.0)
    assert np.all(z2 == -5.0)


def _brute_dilate(z, r, g, ball):
    out = np.full_like(z, -np.inf)
    ky, kx = int(r / g.ry), int(r / g.rx)
    for j in range(z.shape[0]):
        for i in range(z.shape[1]):
            for dy in range(-ky, ky + 1):
                for dx in range(-kx, kx + 1):
                    d2 = (dx * g.rx) ** 2 + (dy * g.ry) ** 2
                    jj, ii = j + dy, i + dx
                    if d2 > r * r or not (0 <= jj < z.shape[0] and 0 <= ii < z.shape[1]):
                        continue
                    drop = r - math.sqrt(r * r - d2) if ball else 0.0
                    out[j, i] = max(out[j, i], z[jj, ii] - drop)
    return out


@pytest.mark.parametrize("ball", [False, True])
def test_dilation_matches_brute_force(ball):
    rng = np.random.default_rng(1)
    g = Grid(0, 0, 0.1, 0.07, 14, 12)
    z = rng.uniform(-1, 0, g.shape)
    fast = (dilate_ball if ball else dilate_flat)(z, 0.33, g)
    assert np.allclose(fast, _brute_dilate(z, 0.33, g, ball))


def test_tip_surface_of_a_spike_and_its_sweep():
    g = Grid(0, 0, 0.1, 0.1, 61, 61)
    target = np.full(g.shape, -5.0)
    target[30, 30] = 0.0  # a 5 mm spike at (3.05, 3.05)
    flat = tip_surface(target, Cutter("flat", 2.0), g)
    xs, ys = np.meshgrid(g.xs, g.ys)
    d = np.hypot(xs - 3.05, ys - 3.05)
    assert np.all(flat[d <= 1.0 - 1e-9] == 0.0) and np.all(flat[d > 1.0 + 1e-9] == -5.0)
    ball = tip_surface(target, Cutter("ball", 2.0), g)
    inside = d <= 1.0 - 1e-9
    assert np.allclose(ball[inside], np.maximum(-(1 - np.sqrt(1 - d[inside] ** 2)), -5.0))
    # allowance: the grown tool stays ≥ 0.5 above the spike
    assert np.all(tip_surface(target, Cutter("flat", 2.0), g, 0.5)[d <= 1.5 - 1e-9] == 0.5)
    # shank: a 1 mm flute tool with a 4 mm shank stays 4 mm (5 − 1) above the floor near it
    sh = tip_surface(target, Cutter("flat", 1.0, flute_length=1.0, shank_diameter=4.0), g)
    ring = (d > 0.6) & (d < 1.9)
    assert np.all(sh[ring] == -1.0)
    # sweeping the tool over its tip surface never cuts below the target
    cut = sweep_surface(np.where(np.isfinite(flat), flat, np.inf), Cutter("flat", 2.0), g)
    assert np.all(cut >= target - 1e-9)


def test_tapered_ball_profile_and_spike():
    h = tapered_ball_height(1.0, 3.92)
    t = math.radians(3.92)
    d_t = math.cos(t)
    # the cone meets the ball tangentially, then rises 1/tan(taper) per mm of radius
    assert h(d_t - 1e-6) == pytest.approx(h(d_t + 1e-6), abs=1e-4)
    assert h(d_t + 0.1) - h(d_t) == pytest.approx(0.1 / math.tan(t))
    assert h(0.5) == pytest.approx(1 - math.sqrt(0.75))
    g = Grid(0, 0, 0.05, 0.05, 101, 101)
    target = np.full(g.shape, -5.0)
    target[50, 50] = 0.0  # a 5 mm spike at (2.525, 2.525)
    tip = tip_surface(target, Cutter("ball", 2.0, taper_deg=3.92), g)
    xs, ys = np.meshgrid(g.xs, g.ys)
    d = np.hypot(xs - 2.525, ys - 2.525)
    assert np.allclose(tip, np.maximum(-np.vectorize(h)(d), -5.0))
    # 5 mm down the cone is ~0.34 mm wider than the ball: the tip stays off the floor there
    assert tip[50, 50 + 25] == pytest.approx(-h(1.25)) and tip[50, 50 + 25] > -5.0
    taper = Cutter("ball", 2.0, taper_deg=3.92)
    cut = sweep_surface(np.where(np.isfinite(tip), tip, np.inf), taper, g)
    assert np.all(cut >= target - 1e-9)


def test_scallop_stepover():
    # 1/8in ball at 0.05 mm scallop: ~0.8 mm (#27)
    assert ops3d.scallop_stepover(3.175, 0.05) == pytest.approx(0.797, abs=1e-3)


def test_rough_levels_add_rest_levels_between_mains():
    lv = ops3d.rough_levels(0.0, -2.0, 1.0, 0.25)
    assert [z for z, _ in lv] == pytest.approx([-0.25, -0.5, -0.75, -1.0, -1.25, -1.5, -1.75, -2.0])
    assert [m for _, m in lv][:4] == [-1.0] * 4


@pytest.mark.parametrize("name", sorted(JOBS))
def test_fast_sim_agrees_with_the_stamp_sim(name):
    post, machine, lib = shapeoko()
    text = post_program(JOBS[name](lib), post, machine)
    a = simulate(text, Heightmap.block((0, 0), (120, 60), 0.0, 0.25))
    b = simulate_fast(text, Heightmap.block((0, 0), (120, 60), 0.0, 0.25))
    # same to the grid: only cells on a tool's edge (one cell) may differ
    assert (np.abs(a.z - b.z) > 0.01).mean() < 0.04


# --- a synthetic section --------------------------------------------------------------


def ellipsoid_halves() -> list[st.Half]:
    """A 40 × 10 × 16 mm ellipsoid 'body' (x, y, z), split on y = 0."""
    m = trimesh.creation.icosphere(subdivisions=4)
    m.apply_scale([20.0, 5.0, 8.0])
    m.apply_translation([20.0, 0.0, 0.0])
    right, left = ch.split_halves(m)
    sil = ch.silhouette_xz(right)
    glue = ch.glue_face_xz(right)
    out = []
    for side, mesh in (("right", right), ("left", left)):
        out.append(st.Half("body", side, sil, ch.max_half_width(mesh), mesh, glue_outline=glue))
    return out


def _cut(lib, table, tool_id, preset=None, mode="aggressive", **over) -> Cut:
    tool = lib.get(tool_id)
    if preset:
        feeds = fd.preset(table, preset, "basswood")
    else:
        feeds = fd.calculate(table, tool.diameter, "basswood", mode, shape=tool.shape)
    return Cut(tool, replace(feeds, **over) if over else feeds)


def make_ctx(finish_tool="ball_3.175", finish_res=0.1, **finish_over) -> st.OpCtx:
    _, _, lib = shapeoko()
    table = fd.load_feeds(ROUTER / "feeds.toml")
    cuts = {
        "pocket": _cut(lib, table, "flat_3.175"),
        "groove": _cut(lib, table, "ball_3.175", mode="conservative"),
        "drill": _cut(lib, table, "flat_3.175", mode="conservative"),
        "profile": _cut(lib, table, "flat_3.175", "owner_contour_flat_3.175"),
        "face": _cut(lib, table, "flat_3.175"),
        "rough3d": _cut(lib, table, "flat_3.175", "owner_rough_flat_3.175"),
    }
    if finish_tool == "ball_3.175":
        so = ops3d.scallop_stepover(3.175, 0.02)
        cuts["finish3d"] = _cut(lib, table, "ball_3.175", stepover=so, **finish_over)
    else:
        cuts["finish3d"] = _cut(lib, table, finish_tool, "owner_finish_flat_0.794", **finish_over)
    e = lib.tools[finish_tool]
    surface = st.Surface3D(
        rough=ops3d.Rough3D(res=0.25, max_stair=0.5),
        finish=ops3d.Finish3D(res=finish_res),
        cutters={
            "rough3d": Cutter("flat", 3.175),
            "finish3d": Cutter(e.shape, e.diameter, e.flute_length, e.shank_diameter),
        },
    )
    return st.OpCtx(cuts, tabs_count=2, tabs_width=3.0, surface=surface)


def make_layout() -> st.Layout:
    return st.plan_layout(ellipsoid_halves(), 8.0, 8.0, 6.0, 0.0)


def _sim_vs_target(texts: list[str], layout: st.Layout, setup: str, res: float = 0.1):
    stock = Heightmap.block((0.0, 0.0), (layout.length, layout.width), 0.0, res)
    simulate_fast(texts, stock)
    ny, nx = stock.z.shape
    wm = ops3d.WorkMesh.of(layout, setup)
    target = raster_top(wm.vertices, wm.faces, Grid(0, 0, res, res, nx, ny), fill=-np.inf)
    return stock.z, target


def _gouge(sim, target) -> float:
    """Deepest cut below the target's 3×3 minimum (grid-aligned walls aren't gouges)."""
    from scipy.ndimage import minimum_filter

    part = np.isfinite(target)
    lo = minimum_filter(np.where(part, target, -1e3), 3, mode="nearest")
    return float(np.max(np.where(part, lo - sim, 0.0)))


def _write(tmp_path, layout, ctx, split: bool):
    post, machine, _ = shapeoko()
    post = post.model_copy(
        update={"output": post.output.model_copy(update={"split_by_tool": split})}
    )
    programs = st.plan("ellipsoid", layout, [], ctx)
    return write_programs(programs, tmp_path, tmp_path, post, machine, below_stock_mm=0.35)


@pytest.mark.parametrize("split", [True, False])
def test_setup2_golden_and_verified(tmp_path, split):
    layout, ctx = make_layout(), make_ctx()
    files = _write(tmp_path, layout, ctx, split)
    names = [f.path for f in files]
    if split:
        assert names == ["setup2_01_flat_3.175.nc", "setup2_02_ball_3.175.nc"]
    else:
        assert names == ["setup2.nc"]
    folder = GOLDEN / ("cnc3d_split" if split else "cnc3d_single")
    for f in files:
        assert f.ok, f.issues
        text = (tmp_path / f.path).read_text()
        n_m6 = len(re.findall(r"^M6T\d+$", text, re.M))
        assert n_m6 == (1 if split else 2)  # every file measures its own tools
        assert not re.search(r"G8[0-3]\D|G10|G92|G19", text)  # plain G1 raster, no G19
        path = folder / f.path
        if os.environ.get("UPDATE_GOLDEN"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        assert text == path.read_text(), f"{path.name} changed; UPDATE_GOLDEN=1 if intended"
    # run order inside setup 2: roughing and cut-out (1/8in flat), then the finish
    ops = [op for f in files for op in f.operations]
    assert ops == [
        "body R: 3D rough",
        "body L: 3D rough",
        "body R: cut-out",
        "body L: cut-out",
        "body R: 3D finish",
        "body L: 3D finish",
    ]


def test_ball_finish_sim_matches_the_halves(tmp_path):
    layout, ctx = make_layout(), make_ctx()
    files = _write(tmp_path, layout, ctx, split=True)
    texts = [(tmp_path / f.path).read_text() for f in files if f.setup == "setup2"]
    sim, target = _sim_vs_target(texts, layout, "setup2")
    floor = st.floor_z(layout, ctx)
    part = np.isfinite(target)
    surface = part & (target > floor + 0.05)
    delta = sim - np.where(part, target, 0.0)
    assert _gouge(sim, target) < 0.03  # no gouge beyond the grid's resolution
    # scallop 0.02 mm, 0.1 mm grid: leftover stays small on the whole surface
    assert np.percentile(delta[surface], 95) < 0.08
    # the ball's radius can't reach into the corner where the wall meets the floor
    assert delta[surface & (target > st.floor_z(layout, ctx) + 1.6)].max() < 0.15
    # roughing alone keeps about the allowance
    rough_only = _sim_vs_target(texts[:1], layout, "setup2")[0] - np.where(part, target, 0)
    assert rough_only[surface].min() > 0.3
    assert np.median(rough_only[surface]) < 0.5 + 0.5 + 0.2


@pytest.mark.slow
def test_flat_finish_owner_defaults(tmp_path):
    """1/32in flat (owner's tool) with a coarser stepover than the owner's 0.076 mm."""
    layout, ctx = make_layout(), make_ctx("flat_0.794", finish_res=0.05, stepover=0.15)
    files = _write(tmp_path, layout, ctx, split=True)
    assert [f.path for f in files][-1] == "setup2_02_flat_0.794.nc"
    assert all(f.ok for f in files)
    texts = [(tmp_path / f.path).read_text() for f in files]
    sim, target = _sim_vs_target(texts, layout, "setup2", res=0.05)
    part = np.isfinite(target)
    surface = part & (target > st.floor_z(layout, ctx) + 0.05)
    delta = sim - np.where(part, target, 0.0)
    assert _gouge(sim, target) < 0.03
    assert np.percentile(delta[surface], 90) < 0.06


def test_cutout_starts_at_the_roughed_depth():
    layout, ctx = make_layout(), make_ctx()
    _, p2 = st.plan("ellipsoid", layout, [], ctx)
    stats = ctx.report["surface3d"]["setup2"]
    for label in ("body R", "body L"):
        start = stats[label]["cutout_start_z"]
        assert -layout.thickness < start < 0.0
        cutout = next(op for op in p2.operations if op.name == f"{label}: cut-out")
        rapids = [mv.z for mv in cutout.moves if mv.kind == "rapid" and mv.z is not None]
        first_z = next(z for z in rapids if z < 5)
        assert first_z == pytest.approx(start + 0.5)


def test_overhang_gets_setup1_3d_ops():
    halves = ellipsoid_halves()
    for h in halves:  # glue face shorter than the side view: an overhang at the front
        h.glue_outline = h.silhouette.difference(
            __import__("shapely.geometry", fromlist=["box"]).box(-1, -9, 6, 9)
        )
    layout = st.plan_layout(halves, 8.0, 8.0, 6.0, 0.0)
    region = st.overhang(layout, layout.halves[0])
    assert region is not None and 15 < region.area < 70
    p1, _ = st.plan("ellipsoid", layout, [], make_ctx())
    assert [op.name for op in p1.operations] == [
        "body R: 3D overhang rough",
        "body L: 3D overhang rough",
        "body R: 3D overhang finish",
        "body L: 3D overhang finish",
    ]
    assert st.overhang(make_layout(), make_layout().halves[0]) is None


def test_setup_sheet_lists_run_order_and_glue_up():
    summary = {
        "variant": "v",
        "machine": "m",
        "material": "basswood",
        "split_by_tool": True,
        "stock_mm": {"length": 100, "width": 50, "thickness": 9.7, "face": 0},
        "zero": "front-left",
        "pins": {"diameter_mm": 6.35},
        "files": [
            {
                "path": "fab/cnc/setup2_01_flat_3.175.nc",
                "setup": "setup2",
                "tools": ["flat_3.175"],
                "operations": ["a R: 3D rough", "a L: 3D rough", "a R: cut-out"],
                "runtime_s": 61,
                "ok": True,
                "issues": [],
            }
        ],
        "tools": {
            "flat_3.175": {
                "name": "1/8in flat",
                "number": 102,
                "diameter_mm": 3.175,
                "shape": "flat",
                "number_verified": True,
            }
        },
        "runtime_s": 61,
        "surface3d": {"floor_z": -8.2},
        "glue_up": ["Ballast: 8 g"],
        "post_glue_up": ["Drill the pin bore"],
        "sim": {
            "setup2": {
                "resolution_mm": 0.15,
                "gouge_max_mm": 0.001,
                "leftover_max_mm": 0.1,
                "leftover_p95_mm": 0.03,
                "leftover_area_frac": 0.02,
                "rim_leftover_max_mm": 1.5,
                "png": "fab/cnc/sim_setup2.png",
            }
        },
        "feeds": {},
        "warnings": [],
    }
    sheet = setup_sheet(summary)
    assert "| 1 | `fab/cnc/setup2_01_flat_3.175.nc` | setup2 | T102 1/8in flat |" in sheet
    assert "3D rough ×2, cut-out" in sheet
    assert "Ballast: 8 g" in sheet and "Drill the pin bore" in sheet
    assert "## Simulation" in sheet and "sim_setup2.png" in sheet
    assert "floor Z -8.2" in sheet
