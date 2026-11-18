import math
from pathlib import Path

import numpy as np
import pytest
from router_jobs import GOLDEN, JOBS, pocket_moves, shapeoko

from luregen.fab.router.ir import Operation, Program, Setup
from luregen.fab.router.post import post_program
from luregen.fab.router.sim import Heightmap, ToolShape, compare, save_png, simulate
from luregen.fab.router.verify import verify


def _clean() -> str:
    return (GOLDEN / "two_tool.nc").read_text()


def _inject(text: str, after: str, new: str) -> str:
    lines = text.splitlines()
    i = lines.index(after)
    return "\n".join([*lines[: i + 1], new, *lines[i + 1 :]]) + "\n"


@pytest.mark.parametrize("name", sorted(JOBS))
def test_goldens_pass(name: str):
    post, machine, _ = shapeoko()
    report = verify((GOLDEN / f"{name}.nc").read_text(), post, machine)
    assert report.ok, report.summary()
    assert report.stock == ((0, 0, -19.05), (120, 60, 0))
    assert report.runtime_s > 0


SEEDED = [
    ("forbidden", lambda t: _inject(t, "G21", "G92X0Y0")),
    ("forbidden", lambda t: _inject(t, "G21", "G81X1Y1Z-1R1F100")),
    ("unsupported", lambda t: _inject(t, "G21", "G61")),
    ("line_length", lambda t: _inject(t, "G21", "(" + "x" * 90 + ")")),
    ("rapid_low", lambda t: _inject(t, "Y23.000", "G0X40.000Y30.000")),
    ("spindle_off", lambda t: _inject(t, "G0Z5.000", "M05")),
    ("below_stock", lambda t: _inject(t, "Y23.000", "Z-25.000")),
    ("outside_stock", lambda t: _inject(t, "Y23.000", "X200.000")),
    ("travel", lambda t: _inject(t, "G21", "G53G0Z-150.000")),
    ("arc_radius", lambda t: _inject(t, "Y23.000", "G2X40.000Y23.000I-4.000J0.100")),
]


@pytest.mark.parametrize(("code", "seed"), SEEDED)
def test_seeded_fault_is_caught(code: str, seed):
    post, machine, _ = shapeoko()
    report = verify(seed(_clean()), post, machine)
    assert code in report.codes(), report.summary()
    assert not report.ok


def test_feed_before_g1_and_missing_end():
    post, machine, _ = shapeoko()
    text = "G90\nG21\nM6T1\nM3S10000\nG0X1Y1Z5\nZ1\nG1Z-1\n"
    report = verify(text, post, machine, stock=((0, 0, -5), (10, 10, 0)))
    assert {"no_feed", "no_end"} <= report.codes()


def test_runtime_estimate():
    post, machine, _ = shapeoko()
    text = "G21\nM3S10000\nG4P2\nG0X0Y0Z5\nG1Z0F600\nX100\nM30\n"
    report = verify(text, post, machine, stock=((0, 0, -5), (100, 10, 0)))
    # 2 s dwell + 5 mm and 100 mm at 600 mm/min; the first rapid starts from unknown XY.
    assert report.runtime_s == pytest.approx(2 + 105 / 10)


def _pocket_program(depth=-4.0) -> Program:
    _, _, lib = shapeoko()
    tool = lib.get("flat_6.35")
    setup = Setup(stock_min=(0, 0, -10), stock_max=(60, 50, 0))
    moves = pocket_moves(20, 15, 40, 35, depth, 3.0, 1200, 300)
    return Program(
        design="pocket",
        setup=setup,
        tools={tool.id: tool},
        operations=[Operation(name="pocket", tool=tool.id, rpm=18000, moves=moves)],
    )


def test_pocket_simulation_matches_analytic_depth():
    post, machine, _ = shapeoko()
    text = post_program(_pocket_program(), post, machine)
    stock = Heightmap.block((0, 0), (60, 50), 0.0, 0.1)
    sim = simulate(text, stock)  # tool shape from the TOOL/MILL comment
    xs, ys = np.meshgrid(sim.xs, sim.ys)
    r = 6.35 / 2
    inner = (xs > 20) & (xs < 40) & (ys > 15) & (ys < 35)
    assert np.allclose(sim.z[inner], -4.0)
    # Analytic pocket: the tool-center rectangle dilated by the tool radius.
    dx = np.maximum(np.maximum(20 - xs, xs - 40), 0)
    dy = np.maximum(np.maximum(15 - ys, ys - 35), 0)
    target = sim.like(np.where(np.hypot(dx, dy) <= r, -4.0, 0.0))
    result = compare(sim, target, tol=1e-9)
    assert result.gouge_max == 0
    # Only cells straddling the boundary may differ (the stepover leaves no ridges).
    assert result.leftover_frac < 0.01
    area = (sim.z < -3.999).sum() * 0.01
    assert area == pytest.approx(20 * 20 + 2 * r * 40 + math.pi * r * r, rel=0.02)


def test_ball_groove_cross_section():
    _, _, lib = shapeoko()
    ball = lib.get("ball_3.175")
    text = "G21\nG0X5Y10Z1\nM3S10000\nG1Z-1F100\nX25\nG0Z5\nM30\n"
    sim = simulate(
        text,
        Heightmap.block((0, 0), (30, 20), 0.0, 0.05),
        default_tool=ToolShape("ball", ball.diameter),
    )
    col = sim.z[:, int(15 / 0.05)]
    r = ball.radius
    for y, z in zip(sim.ys, col, strict=True):
        d = abs(y - 10)
        expect = min(0.0, -1 + r - math.sqrt(r * r - d * d)) if d <= r else 0.0
        assert z == pytest.approx(expect, abs=2e-3)


def test_gouge_is_detected_and_png_written(tmp_path: Path):
    post, machine, _ = shapeoko()
    good = post_program(_pocket_program(), post, machine)
    target = simulate(good, Heightmap.block((0, 0), (60, 50), 0.0, 0.2))
    bad = post_program(_pocket_program(depth=-4.5), post, machine)
    sim = simulate(bad, Heightmap.block((0, 0), (60, 50), 0.0, 0.2))
    result = compare(sim, target)
    assert not result.ok and result.gouge_max == pytest.approx(0.5)
    png = save_png(tmp_path / "dev.png", sim, result, "seeded gouge")
    assert png.stat().st_size > 1000
