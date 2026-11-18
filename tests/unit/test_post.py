"""Post-processor golden files and properties. Regenerate goldens: UPDATE_GOLDEN=1 pytest."""

import math
import os

import numpy as np
import pytest
from router_jobs import GOLDEN, JOBS, shapeoko

from luregen.fab.gcode import PLANE_AXES, Motion, arc_sweep, interpret, parse
from luregen.fab.router.ir import Arc, Comment, Feed, Operation, Program, Rapid, Setup
from luregen.fab.router.post import PostError, post_program, sanitize_comment


def _arcs(text: str) -> list[Motion]:
    return [e for e in interpret(parse(text)) if isinstance(e, Motion) and e.is_arc]


def _radii(m: Motion) -> tuple[float, float]:
    a, b, _ = PLANE_AXES[m.plane]
    assert m.center is not None
    r0 = math.hypot(m.start[a] - m.center[a], m.start[b] - m.center[b])
    r1 = math.hypot(m.end[a] - m.center[a], m.end[b] - m.center[b])
    return r0, r1


@pytest.mark.parametrize("name", sorted(JOBS))
def test_golden(name: str):
    post, machine, lib = shapeoko()
    text = post_program(JOBS[name](lib), post, machine)
    path = GOLDEN / f"{name}.nc"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(text)
    assert text == path.read_text(), f"{path.name} changed; rerun with UPDATE_GOLDEN=1 if intended"


@pytest.mark.parametrize("name", sorted(JOBS))
def test_golden_has_no_forbidden_codes_and_short_lines(name: str):
    post, _, _ = shapeoko()
    text = (GOLDEN / f"{name}.nc").read_text()
    for line in parse(text):
        assert len(line.raw) <= post.max_line_length
        for w in line.words:
            if w.letter in "GM":
                assert not any(w.code == f or w.code.startswith(f + ".") for f in post.forbid)


def test_carbide_style_header_and_tool_change():
    post, machine, lib = shapeoko()
    lines = post_program(JOBS["two_tool"](lib), post, machine).splitlines()
    assert lines[0] == "(Design File: golden two tool)"
    assert lines[2] == "(stockMin:0.00mm, 0.00mm, -19.05mm)"
    assert {"G90", "G21", "G53G0Z-5.000"} <= set(lines)
    i = lines.index("M6T201")
    assert lines[i - 3 : i] == ["M05", "G53G0Z-5.000", "(TOOL/MILL,6.35, 0.00, 0.00, 0.00)"]
    assert lines[i + 1 : i + 3] == ["M03S18000", "G4P3.00"]
    # Every tool, the first included, gets the full block so the BitSetter measures it.
    assert lines.count("M05") == 3 and "M6T101" in lines and lines[-1] == "M30"
    assert "M0" in lines


def _one_arc_program(arc: Arc, start: Rapid) -> Program:
    tool = shapeoko()[2].get("ball_3.175")
    return Program(
        design="arc",
        setup=Setup(stock_min=(-50, -50, -20), stock_max=(50, 50, 0)),
        tools={tool.id: tool},
        operations=[Operation(name="a", tool=tool.id, rpm=10000, moves=[start, arc])],
    )


def test_plane_set_controls_arc_output():
    post, machine, _ = shapeoko()
    lead = Arc(z=0, x=1, cz=0, cx=0, plane="zx", cw=True, f=500)
    text = post_program(_one_arc_program(lead, Rapid(x=0, y=0, z=1)), post, machine)
    assert "G18" in text and "K-1.000" in text
    yz = Arc(y=1, z=0, cy=0, cz=0, plane="yz", cw=True, f=500)
    text = post_program(_one_arc_program(yz, Rapid(x=0, y=0, z=1)), post, machine)
    assert "G19" not in text and "G2" not in text.split("(a)")[1]


def test_trim_zeros_style():
    post, machine, _ = shapeoko()
    dec = post.decimals.model_copy(update={"trim_zeros": True, "feed": 0})
    fusion = post.model_copy(update={"decimals": dec, "word_separator": " "})
    text = post_program(
        _one_arc_program(Feed(x=50.8, y=0, f=2032), Rapid(x=0, y=0, z=5)), fusion, machine
    )
    assert {"G0 X0 Y0 Z5", "G1 X50.8 F2032"} <= set(text.splitlines())


def test_profile_arcs_are_split_snapped_and_linearized():
    post, machine, lib = shapeoko()
    text = post_program(JOBS["profile_arcs"](lib), post, machine)
    arcs = _arcs(text)
    # 8 corner arcs + full circle in 2 halves; the two tiny arcs are linearized.
    assert len(arcs) == 10
    for m in arcs:
        r0, r1 = _radii(m)
        assert r0 >= 0.1 and abs(r0 - r1) <= 0.002
        assert arc_sweep(m) <= math.pi + 1e-9


def test_peck_drill_expands_into_pecks():
    post, machine, lib = shapeoko()
    text = post_program(JOBS["peck_drill"](lib), post, machine)
    feeds_z = [
        e.end[2]
        for e in interpret(parse(text))
        if isinstance(e, Motion) and e.mode == "feed" and e.start[:2] == (10.0, 10.0)
    ]
    assert feeds_z == [-1.0, -4.0, -7.0, -8.0]
    assert "G4P0.50" in text.splitlines()


def test_modal_suppression():
    post, machine, lib = shapeoko()
    lines = post_program(JOBS["two_tool"](lib), post, machine).splitlines()
    body = lines[lines.index("(Ballast pocket)") :]
    assert sum(ln.startswith("G1") for ln in body) <= 4  # G1 only when the mode changes
    assert "X60.000F1200.0" in body and "Y23.000" in body


def _random_arc_program(rng: np.random.Generator, n: int = 60) -> Program:
    tool_lib = shapeoko()[2]
    tool = tool_lib.get("flat_3.175")
    moves: list = [Rapid(x=0, y=0, z=5)]
    for _ in range(n):
        cx, cy = rng.uniform(-200, 200, 2)
        r = 10 ** rng.uniform(-1.5, 2.3)
        t0, sweep = rng.uniform(0, 2 * math.pi), rng.uniform(0.001, 2 * math.pi)
        cw = bool(rng.integers(2))
        t1 = t0 - sweep if cw else t0 + sweep
        start = (cx + r * math.cos(t0), cy + r * math.sin(t0))
        end = (cx + r * math.cos(t1), cy + r * math.sin(t1))
        moves += [
            Rapid(x=start[0], y=start[1]),
            Feed(z=float(rng.uniform(-5, 0)), f=300),
            Arc(x=end[0], y=end[1], cx=cx, cy=cy, cw=cw, f=900, z=float(rng.uniform(-5, 0))),
            Rapid(z=5),
        ]
    setup = Setup(stock_min=(-400, -400, -20), stock_max=(400, 400, 0))
    return Program(
        design="random arcs",
        setup=setup,
        tools={tool.id: tool},
        operations=[Operation(name="arcs", tool=tool.id, rpm=10000, moves=moves)],
    )


@pytest.mark.parametrize("seed", range(5))
def test_property_arc_endpoints_lie_on_circle(seed: int):
    post, machine, _ = shapeoko()
    text = post_program(_random_arc_program(np.random.default_rng(seed)), post, machine)
    arcs = _arcs(text)
    assert arcs
    for m in arcs:
        r0, r1 = _radii(m)
        assert abs(r0 - r1) <= 0.002, (m.line, r0, r1)
        assert r0 >= post.arcs.min_radius - 1e-9
        assert arc_sweep(m) <= math.radians(post.arcs.max_sweep_deg) + 1e-6
        a, b, _ = PLANE_AXES[m.plane]
        assert math.dist(m.start[:2], m.end[:2]) >= post.arcs.min_chord - 0.002


@pytest.mark.parametrize("seed", range(3))
def test_property_no_forbidden_codes(seed: int):
    post, machine, _ = shapeoko()
    text = post_program(_random_arc_program(np.random.default_rng(seed), 20), post, machine)
    codes = {w.code for ln in parse(text) for w in ln.words if w.letter in "GM"}
    assert not codes & set(post.forbid)
    assert codes <= set(machine.controller.supported)


def test_forbidden_code_in_template_is_rejected():
    post, machine, lib = shapeoko()
    bad = post.model_copy(update={"footer": ["G92 X0", "M30"]})
    with pytest.raises(PostError, match="forbidden code G92"):
        post_program(JOBS["peck_drill"](lib), bad, machine)


def test_long_comment_is_sanitized_and_capped():
    post, _, _ = shapeoko()
    out = sanitize_comment("C:\\Users\\me (draft) é " + "x" * 200, post)
    assert out.startswith("(C:_Users_me _draft_ _ x") and out.endswith(")")
    assert len(out) <= post.comments.max_length and out.count("(") == 1


def test_comment_moves_cannot_break_lines():
    post, machine, lib = shapeoko()
    program = JOBS["peck_drill"](lib)
    program.operations[0].moves.insert(0, Comment(text="a) G92 (b" + "y" * 100))
    text = post_program(program, post, machine)
    assert all(len(ln) <= 80 for ln in text.splitlines())


def test_arc_far_off_its_circle_is_a_cam_error():
    post, machine, lib = shapeoko()
    tool = lib.get("flat_6.35")
    program = Program(
        design="bad",
        setup=Setup(stock_min=(0, 0, -5), stock_max=(50, 50, 0)),
        tools={tool.id: tool},
        operations=[
            Operation(
                name="x",
                tool=tool.id,
                rpm=10000,
                moves=[
                    Rapid(x=10, y=0, z=1),
                    Arc(x=0, y=10.5, cx=0, cy=0, cw=False, f=500),
                ],
            )
        ],
    )
    with pytest.raises(PostError, match="off its circle"):
        post_program(program, post, machine)
