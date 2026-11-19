"""Setup planner: layout, part → work transforms, the flip, programs and per-tool output."""

import re

import numpy as np
import pytest
from router_jobs import ROUTER, shapeoko
from shapely.geometry import LineString, Point, Polygon

from luregen.fab.router.cam import setups as st
from luregen.fab.router.cam.job import split_by_tool, write_programs
from luregen.fab.router.cam.ops2d import Cut
from luregen.fab.router.feeds import calculate, load_feeds
from luregen.geometry.cnc_halves import rounded_rect


def ellipse(cx, cz, a, b, n=180) -> Polygon:
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return Polygon(np.column_stack([cx + a * np.cos(t), cz + b * np.sin(t)]))


def halves() -> list[st.Half]:
    out = []
    for name, sil in (("head", ellipse(40, 2, 40, 22)), ("tail", ellipse(130, 0, 40, 18))):
        for side in ("right", "left"):
            out.append(st.Half(name, side, sil, 8.0))
    return out


@pytest.fixture
def layout() -> st.Layout:
    return st.plan_layout(halves(), 20, 10, 9.5, 0.0, pin_d=6.35, pin_depth=3.0)


@pytest.fixture(scope="module")
def ctx() -> st.OpCtx:
    table = load_feeds(ROUTER / "feeds.toml")
    _, _, lib = shapeoko()
    cuts = {}
    for kind, tool_id, mode in [
        ("pocket", "flat_3.175", "aggressive"),
        ("groove", "ball_3.175", "conservative"),
        ("drill", "flat_3.175", "conservative"),
        ("profile", "flat_3.175", "aggressive"),
        ("face", "flat_3.175", "aggressive"),
    ]:
        tool = lib.get(tool_id)
        cuts[kind] = Cut(tool, calculate(table, tool.diameter, "basswood", mode, shape=tool.shape))
    return st.OpCtx(cuts)


def features() -> list[st.GlueFeature]:
    return [
        st.GlueFeature("head", "pocket", "ballast", rounded_rect(50, -10, 16, 6, 1.85), 1.6),
        st.GlueFeature("head", "groove", "wire", LineString([(-3, -4), (83, -4)]), 0.67),
        st.GlueFeature("tail", "groove", "wire", LineString([(87, -4), (173, -4)]), 0.67),
    ]


def test_layout_fits_halves_in_stock(layout):
    assert layout.length == pytest.approx(20 + 80 + 10 + 80 + 20)
    assert layout.width == pytest.approx(2 * 20 + 2 * 44 + 10)
    for setup in st.SETUP_NAMES:
        for h in layout.halves:
            x0, y0, x1, y1 = layout.to_work(h, h.silhouette, setup).bounds
            assert x0 >= 20 - 1e-9 and x1 <= layout.length - 20 + 1e-9
            assert y0 >= 20 - 1e-9 and y1 <= layout.width - 20 + 1e-9
    a = {
        h.side: layout.to_work(h, h.silhouette, "setup1")
        for h in layout.halves
        if h.section == "head"
    }
    assert not a["right"].intersects(a["left"])


@pytest.mark.parametrize("setup", st.SETUP_NAMES)
def test_transforms_are_proper_rotations(layout, setup):
    for h in layout.halves:
        m = layout.matrix(h, setup)
        r = m[:3, :3]
        assert np.allclose(r @ r.T, np.eye(3)) and np.linalg.det(r) == pytest.approx(1.0)


def test_glue_face_up_then_outer_face_up(layout):
    for h in layout.halves:
        m1, m2 = layout.matrix(h, "setup1"), layout.matrix(h, "setup2")
        glue = np.array([40.0, 0.0, 0.0, 1.0])
        outer = np.array([40.0, h.s * h.half_width, 0.0, 1.0])  # outer apex of this half
        assert (m1 @ glue)[2] == pytest.approx(0.0)  # glue face = stock top in setup 1
        assert (m1 @ outer)[2] == pytest.approx(-h.half_width)
        assert (m2 @ glue)[2] == pytest.approx(-layout.thickness)  # on the spoilboard
        assert (m2 @ outer)[2] == pytest.approx(-layout.thickness + h.half_width)
        # setup 2 = flip ∘ setup 1, and to_work agrees with the matrix
        assert np.allclose(m2, layout.flip_matrix() @ m1)
        p = layout.to_work(h, Point(40.0, 5.0), "setup2")
        assert np.allclose([p.x, p.y], (m2 @ [40.0, 0.0, 5.0, 1.0])[:2])


def test_flip_keeps_pins_and_registers_features(layout):
    for x, y in layout.pins:
        fx, fy, _ = layout.flip_point(x, y, 0.0)
        assert (fx, fy) == pytest.approx((x, y))
    # a glue-face pocket cut in setup 1 lies inside its half's outline in setup 2
    pocket = features()[0].geom
    for h in layout.halves:
        if h.section != "head":
            continue
        c = layout.to_work(h, pocket.centroid, "setup1")
        fx, fy, _ = layout.flip_point(c.x, c.y, 0.0)
        assert layout.to_work(h, h.silhouette, "setup2").contains(Point(fx, fy))
        assert layout.to_work(h, pocket, "setup2").centroid.distance(Point(fx, fy)) < 1e-6


def test_plan_two_setups(layout, ctx):
    p1, p2 = st.plan("test", layout, features(), ctx)
    assert p1.setup.name == "setup1" and p2.setup.name == "setup2"
    assert p1.setup.stock_min == (0.0, 0.0, -9.5)
    names1 = [op.name for op in p1.operations]
    assert names1 == [
        "head R: ballast pocket",
        "head L: ballast pocket",
        "Flip pin hole 1",
        "Flip pin hole 2",
        "head R: wire groove",
        "head L: wire groove",
        "tail R: wire groove",
        "tail L: wire groove",
    ]
    assert [op.tool for op in p1.operations] == ["flat_3.175"] * 4 + ["ball_3.175"] * 4
    assert [op.name for op in p2.operations] == [f"{h.label}: cut-out" for h in layout.halves]


def test_setup2_hook_runs_before_cutout(layout, ctx, monkeypatch):
    def three_d(lay, c):
        return [st._op("3D finish", "pocket", c, [])]

    monkeypatch.setattr(st, "SETUP2_3D_OPS", [three_d])
    _, p2 = st.plan("test", layout, [], ctx)
    assert p2.operations[0].name == "3D finish"


def _write(tmp_path, layout, ctx, split: bool):
    post, machine, _ = shapeoko()
    post = post.model_copy(
        update={"output": post.output.model_copy(update={"split_by_tool": split})}
    )
    programs = st.plan("test", layout, features(), ctx)
    return write_programs(programs, tmp_path, tmp_path, post, machine, below_stock_mm=3.1)


def test_split_by_tool_is_the_default():
    post, _, _ = shapeoko()
    assert post.output.split_by_tool is True


def test_one_file_per_tool(tmp_path, layout, ctx):
    files = _write(tmp_path, layout, ctx, split=True)
    assert [f.path for f in files] == [
        "setup1_01_flat_3.175.nc",
        "setup1_02_ball_3.175.nc",
        "setup2_01_flat_3.175.nc",
    ]
    for f in files:
        assert f.ok, f.issues
        text = (tmp_path / f.path).read_text()
        assert len(re.findall(r"^M6T\d+$", text, re.M)) == 1  # its own BitSetter block
        assert text.startswith("(Design File: test)")
        assert not re.search(r"G8[0-3]\D|G10|G92", text)


def test_single_program_per_setup(tmp_path, layout, ctx):
    files = _write(tmp_path, layout, ctx, split=False)
    assert [f.path for f in files] == ["setup1.nc", "setup2.nc"]
    assert all(f.ok for f in files)
    text = (tmp_path / "setup1.nc").read_text()
    assert re.findall(r"^M6T(\d+)$", text, re.M) == ["102", "101"]


def test_split_by_tool_groups_consecutive_ops(layout, ctx):
    p1, _ = st.plan("test", layout, features(), ctx)
    parts = split_by_tool(p1)
    assert [t for t, _ in parts] == ["flat_3.175", "ball_3.175"]
    assert sum(len(p.operations) for _, p in parts) == len(p1.operations)
    assert all(set(p.tools) == {t} for t, p in parts)


def test_wire_paths_and_groove_size():
    sil = ellipse(40, 2, 40, 22)
    (path,) = st.wire_paths(sil, -4.0, 3.0)
    x0, x1 = path.coords[0][0], path.coords[-1][0]
    half = 40 * np.sqrt(1 - (6 / 22) ** 2)  # ellipse half-length at z = -4
    assert (x0, x1) == pytest.approx((40 - half - 3, 40 + half + 3), abs=0.05)
    assert st.wire_paths(sil, 40.0, 3.0) == []
    d = st.groove_depth(1.04, 0.15)
    assert d == pytest.approx(0.67)
    assert st.ball_groove_width(1.5875, d) > 1.04  # the wire fits the channel


@pytest.mark.parametrize(
    ("need", "step", "want"),
    [
        (9.63, 6.35, 12.7),  # the sample: half width 8.63 + 1 mm top -> a 1/2in board
        (12.7, 6.35, 12.7),  # an exact fit doesn't round up
        (12.71, 6.35, 19.05),
        (9.63, 0.0, 9.7),  # step 0: next 0.1 mm, as before
    ],
)
def test_auto_stock_thickness_rounds_up_to_board_increment(need, step, want):
    from luregen.fab.router.cam.job import auto_thickness

    assert auto_thickness(need, step) == pytest.approx(want)
