"""Toolpath IR jobs shared by the post, verify and simulation tests."""

import math
from pathlib import Path

from luregen.fab.router.ir import (
    Arc,
    Comment,
    Drill,
    Feed,
    Operation,
    Pause,
    Program,
    Rapid,
    Setup,
)
from luregen.fab.router.post_config import (
    MachineConfig,
    PostConfig,
    ToolLibrary,
    load_machine,
    load_post,
    load_tools,
)

REPO = Path(__file__).resolve().parents[1]
ROUTER = REPO / "spec" / "routers" / "shapeoko3_xxl"
GOLDEN = Path(__file__).resolve().parent / "golden"


def shapeoko() -> tuple[PostConfig, MachineConfig, ToolLibrary]:
    return (
        load_post(ROUTER / "post.toml"),
        load_machine(ROUTER / "machine.toml"),
        load_tools(ROUTER / "tools.toml"),
    )


def _setup(name: str = "setup1") -> Setup:
    return Setup(name=name, stock_min=(0, 0, -19.05), stock_max=(120, 60, 0), safe_z=5.0)


def rounded_rect_pass(x0, y0, x1, y1, r, z, f) -> list:
    """Climb-cut rounded rectangle outline starting mid bottom edge, at depth z."""
    xm = (x0 + x1) / 2
    return [
        Feed(x=x1 - r, y=y0, f=f),
        Arc(x=x1, y=y0 + r, cx=x1 - r, cy=y0 + r, cw=False, f=f),
        Feed(y=y1 - r, f=f),
        Arc(x=x1 - r, y=y1, cx=x1 - r, cy=y1 - r, cw=False, f=f),
        Feed(x=x0 + r, f=f),
        Arc(x=x0, y=y1 - r, cx=x0 + r, cy=y1 - r, cw=False, f=f),
        Feed(y=y0 + r, f=f),
        Arc(x=x0 + r, y=y0, cx=x0 + r, cy=y0 + r, cw=False, f=f),
        Feed(x=xm, z=z, f=f),
    ]


def profile_with_arcs(lib: ToolLibrary) -> Program:
    """Rounded-rectangle profile in two passes, a full-circle hole and two tiny arcs."""
    tool = lib.get("flat_6.35")
    moves: list = [Rapid(x=60, y=10, z=5), Feed(z=0.5, f=500)]
    for z in (-3.0, -6.0):
        moves += [Feed(z=z, f=250)]
        moves += rounded_rect_pass(20, 10, 100, 50, 8, z, 1200)
    moves += [Rapid(z=5)]
    # Full circle (split into two 180° blocks), entered on its rightmost point.
    moves += [Rapid(x=70, y=30), Feed(z=-2, f=250), Arc(x=70, y=30, cx=60, cy=30, f=1000)]
    # Tiny-radius arc (r < 0.1) and a short-chord arc (< 0.25 mm): both linearized.
    moves += [
        Feed(x=60.05, y=30, f=1000),
        Arc(x=59.95, y=30, cx=60, cy=30, cw=False, f=1000),
        Feed(x=55, y=30, f=1000),
        Arc(x=60 - 5 * math.cos(0.04), y=30 + 5 * math.sin(0.04), cx=60, cy=30, f=1000),
        Rapid(z=5),
    ]
    return Program(
        design="golden profile",
        setup=_setup(),
        tools={tool.id: tool},
        operations=[Operation(name="Profile outline", tool=tool.id, rpm=18000, moves=moves)],
    )


def peck_drill(lib: ToolLibrary) -> Program:
    tool = lib.get("flat_3.175")
    holes = [(10.0, 10.0), (110.0, 10.0), (110.0, 50.0)]
    moves: list = [Comment(text="Registration pins (waste area)")]
    moves += [Drill(x=x, y=y, r_z=2, bottom_z=-8, peck=3, f=200) for x, y in holes]
    moves += [Drill(x=10, y=50, r_z=2, bottom_z=-4, f=200, dwell_s=0.5), Rapid(z=5)]
    return Program(
        design="golden drill",
        setup=_setup(),
        tools={tool.id: tool},
        operations=[Operation(name="Peck drill pins", tool=tool.id, rpm=20000, moves=moves)],
    )


def pocket_moves(x0, y0, x1, y1, depth, stepover, f, plunge_f, safe_z=5.0) -> list:
    """Zigzag raster of tool-center lines covering [x0, x1] × [y0, y1] at `depth`."""
    moves: list = [Rapid(z=safe_z), Rapid(x=x0, y=y0), Rapid(z=1), Feed(z=depth, f=plunge_f)]
    y, forward = y0, True
    while True:
        moves.append(Feed(x=x1 if forward else x0, f=f))
        if y >= y1 - 1e-9:
            break
        y = min(y + stepover, y1)
        moves.append(Feed(y=y, f=f))
        forward = not forward
    moves.append(Rapid(z=safe_z))
    return moves


def two_tool(lib: ToolLibrary) -> Program:
    """1/4in flat pocket, then a 1/8in ball groove; a pause between the tools."""
    flat, ball = lib.get("flat_6.35"), lib.get("ball_3.175")
    pocket = pocket_moves(30, 20, 60, 40, -4.0, 3.0, 1200, 300)
    groove = [
        Pause(message="Check pocket floor"),
        Rapid(x=10, y=30),
        Rapid(z=1),
        Feed(z=-1.5875, f=200),
        Feed(x=110, f=800),
        Rapid(z=5),
    ]
    return Program(
        design="golden two tool",
        setup=_setup(),
        tools={flat.id: flat, ball.id: ball},
        operations=[
            Operation(name="Ballast pocket", tool=flat.id, rpm=18000, moves=pocket),
            Operation(name="Wire groove", tool=ball.id, rpm=24000, moves=groove),
        ],
    )


JOBS = {"profile_arcs": profile_with_arcs, "peck_drill": peck_drill, "two_tool": two_tool}
