"""2.5D CAM ops: geometry checked by the heightmap simulation, G-code by verify (#41)."""

import math
from dataclasses import replace

import numpy as np
import pytest
import shapely
from router_jobs import ROUTER, shapeoko
from shapely.geometry import LineString, Point
from shapely.geometry import box as shp_box

from luregen.fab.router.cam import ops2d
from luregen.fab.router.cam.ops2d import CamError, Cut, Tabs, levels
from luregen.fab.router.feeds import calculate, load_feeds
from luregen.fab.router.ir import Arc, Drill, Operation, Program, Setup
from luregen.fab.router.post import post_program
from luregen.fab.router.sim import Heightmap, ToolShape, simulate
from luregen.fab.router.verify import verify
from luregen.geometry.cnc_halves import rounded_rect

STOCK = ((0.0, 0.0, -12.0), (80.0, 50.0, 0.0))
RES = 0.1


@pytest.fixture(scope="module")
def table():
    return load_feeds(ROUTER / "feeds.toml")


def cut(table, tool_id="flat_3.175", mode="aggressive", **kw) -> Cut:
    _, _, lib = shapeoko()
    tool = lib.get(tool_id)
    feeds = calculate(table, tool.diameter, "basswood", mode, shape=tool.shape)
    return Cut(tool=tool, feeds=feeds, **kw)


def post_and_sim(moves, c: Cut, below_stock=0.5) -> tuple[str, Heightmap]:
    program = Program(
        design="ops test",
        setup=Setup(stock_min=STOCK[0], stock_max=STOCK[1]),
        tools={c.tool.id: c.tool},
        operations=[Operation(name="op", tool=c.tool.id, rpm=c.feeds.rpm, moves=moves)],
    )
    post, machine, _ = shapeoko()
    text = post_program(program, post, machine)
    report = verify(text, post, machine, below_stock_mm=below_stock)
    assert report.ok, report.summary()
    for code in ("G80", "G81", "G82", "G83", "G10", "G92"):
        assert code not in text
    hm = Heightmap.block(STOCK[0][:2], STOCK[1][:2], 0.0, RES)
    simulate(text, hm, default_tool=ToolShape(c.tool.shape, c.tool.diameter))
    return text, hm


def cells(hm: Heightmap, geom):
    """Heights of the cells whose centres lie in `geom`."""
    xs, ys = np.meshgrid(hm.xs, hm.ys)
    mask = shapely.contains_xy(geom, xs, ys)
    return hm.z[mask]


def test_levels():
    assert levels(0, -3, 1.5875) == pytest.approx([-1.5, -3.0])
    assert levels(0, -1, 2.0) == [-1.0]
    assert levels(0, 0, 1.0) == []


def test_pocket_clears_region_to_flat_floor(table):
    c = cut(table)  # aggressive: stepover 2.22 mm > tool radius, so the core needs care
    region = rounded_rect(40, 25, 30, 14, c.r + 0.25)
    _, hm = post_and_sim(ops2d.pocket(region, 0.0, -3.0, c), c)
    inside = cells(hm, region.buffer(-0.15))
    assert inside.max() == pytest.approx(-3.0, abs=1e-6)  # no island left
    assert inside.min() == pytest.approx(-3.0, abs=1e-6)  # flat floor, no gouge
    assert cells(hm, shp_box(0, 0, 80, 50).difference(region.buffer(0.15))).min() == 0.0


def test_pocket_ramps_in(table):
    c = cut(table)
    region = rounded_rect(40, 25, 30, 14, c.r + 0.25)
    moves = ops2d.pocket(region, 0.0, -1.5, c)
    plunges = [
        m for m in moves if getattr(m, "z", None) is not None and m.kind == "feed" and m.x is None
    ]
    # only the approach feeds straight down (to the surface); the depth comes from a ramp
    assert all(m.z >= 0.0 for m in plunges)
    assert min(m.z for m in moves if getattr(m, "z", None) is not None) == pytest.approx(-1.5)


def test_pocket_narrower_than_tool(table):
    c = cut(table)
    with pytest.raises(CamError, match="narrower"):
        ops2d.pocket(shp_box(10, 10, 12.5, 30), 0.0, -1.0, c)


def test_profile_with_tabs(table):
    c = cut(table, mode="conservative")
    c = Cut(c.tool, replace(c.feeds, doc=1.016))
    part = rounded_rect(40, 25, 50, 26, 8)
    bottom, tab_top = -12.3, -12.0 + 1.5
    tabs = Tabs(count=4, width=5.0, top_z=tab_top)
    _, hm = post_and_sim(ops2d.profile(part, 0.0, bottom, c, tabs), c, below_stock=0.5)
    path = part.buffer(c.r, quad_segs=16).exterior
    zs = np.array(
        [
            hm.z[int((p.y - hm.y0) / RES), int((p.x - hm.x0) / RES)]
            for p in (path.interpolate(d) for d in np.linspace(0, path.length, 400, endpoint=False))
        ]
    )
    assert zs.min() == pytest.approx(bottom)
    on_tab = np.isclose(zs, tab_top, atol=1e-6)
    # four separate tabs; the path lifts over width + tool d, so `width` of material stays
    runs = np.count_nonzero(np.diff(on_tab.astype(int)) == 1) + (on_tab[0] and not on_tab[-1])
    assert runs == 4
    frac = on_tab.mean()
    assert frac == pytest.approx(4 * 5.0 / path.length, abs=0.01)
    assert cells(hm, part.buffer(-0.2)).min() == 0.0  # the part itself is untouched


def test_too_many_tabs(table):
    c = cut(table)
    with pytest.raises(CamError, match="tabs"):
        ops2d.profile(rounded_rect(40, 25, 10, 8, 2), 0, -3, c, Tabs(8, 5.0, -2.0))


def test_ball_groove_depth_and_width(table):
    c = cut(table, "ball_3.175", "conservative")
    depth = 0.67
    _, hm = post_and_sim(ops2d.groove(LineString([(5, 25), (75, 25)]), 0.0, -depth, c), c)
    assert hm.z.min() == pytest.approx(-depth, abs=1e-3)
    row = hm.z[int((25 - hm.y0) / RES)]
    assert row[100:600].max() == pytest.approx(-depth, abs=0.01)
    width = (hm.z[:, 400] < -1e-6).sum() * RES
    r = c.tool.radius
    assert width == pytest.approx(2 * math.sqrt(2 * r * depth - depth**2), abs=2 * RES)


def test_helical_bore(table):
    c = cut(table, mode="conservative")
    moves = ops2d.hole(40, 25, 6.35, 0.0, -12.0 - 3.0, c)
    assert any(isinstance(m, Arc) and m.z is not None for m in moves)  # helix
    text, hm = post_and_sim(moves, c, below_stock=3.1)
    assert "G2" in text or "G3" in text
    assert cells(hm, Point(40, 25).buffer(6.35 / 2 - 0.15)).max() == pytest.approx(-15.0)
    assert cells(hm, shp_box(0, 0, 80, 50).difference(Point(40, 25).buffer(3.4))).min() == 0


def test_tool_sized_hole_is_a_peck_drill(table):
    c = cut(table, mode="conservative")
    moves = ops2d.hole(40, 25, 3.175, 0.0, -5.0, c)
    assert any(isinstance(m, Drill) for m in moves)
    text, hm = post_and_sim(moves, c)
    assert hm.z.min() == pytest.approx(-5.0)
    with pytest.raises(CamError):
        ops2d.hole(40, 25, 2.0, 0.0, -5.0, c)


def test_face(table):
    c = cut(table)
    rect = shp_box(10, 10, 70, 40)
    _, hm = post_and_sim(ops2d.face(rect, 0.0, -0.5, c), c)
    assert cells(hm, rect).max() == pytest.approx(-0.5)
