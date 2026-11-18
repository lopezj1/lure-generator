import math

import pytest

from luregen.fab.gcode import (
    GcodeSyntaxError,
    Motion,
    Spindle,
    ToolChange,
    arc_points,
    interpret,
    motion_length,
    parse,
    tokenize_line,
)


def test_tokenize_packed_and_spaced_words():
    packed = tokenize_line("G1Z-1.587F1016.0")
    assert [w.letter + w.text for w in packed.words] == ["G1", "Z-1.587", "F1016.0"]
    assert packed.separated is False and packed.get("Z").decimals == 3
    spaced = tokenize_line("G18 G2 X-109.696 Z-1.194 I0.317 K0 (lead in) ; tail")
    assert spaced.separated is True and spaced.comments == ["lead in", "tail"]
    assert spaced.get("K").decimals == 0 and spaced.codes("G") == ["G18", "G2"]


def test_codes_are_canonical():
    line = tokenize_line("M03S19600")
    assert line.has_code("M3") and line.get("S").value == 19600
    assert tokenize_line("G43.1 Z1").codes("G") == ["G43.1"]


@pytest.mark.parametrize("bad", ["G1 X", "G1 X1 $", "(open"])
def test_syntax_errors(bad: str):
    with pytest.raises(GcodeSyntaxError):
        tokenize_line(bad)


def test_interpreter_tracks_state_and_machine_moves():
    text = (
        "G21 G90\nM6 T5\nM3 S1000\nG53 G0 Z-5\nG0 X1 Y2\nZ1\nG1 Z-1 F100\nG91 X2\nG20 G90 G1 X1\n"
    )
    events = list(interpret(parse(text)))
    assert isinstance(events[0], ToolChange) and events[0].tool == 5
    assert isinstance(events[1], Spindle) and events[1].on
    motions = [e for e in events if isinstance(e, Motion)]
    assert motions[0].machine and math.isinf(motions[0].end[2])
    assert motions[2].end == (1, 2, 1) and motions[3].feed == 100 and motions[3].spindle_on
    assert motions[4].end == (3, 2, -1)
    assert motions[5].end[0] == pytest.approx(25.4)


def test_arcs_ij_and_r_forms_agree():
    ij = [
        e for e in interpret(parse("G0 X10 Y0 Z0\nG3 X0 Y10 I-10 J0 F100")) if isinstance(e, Motion)
    ]
    r = [e for e in interpret(parse("G0 X10 Y0 Z0\nG3 X0 Y10 R10 F100")) if isinstance(e, Motion)]
    assert ij[1].center == pytest.approx((0, 0, 0)) and r[1].center == pytest.approx((0, 0, 0))
    assert motion_length(ij[1]) == pytest.approx(math.pi * 5)
    pts = arc_points(ij[1], 0.01)
    assert all(math.hypot(p[0], p[1]) == pytest.approx(10) for p in pts)
    assert pts[-1] == (0, 10, 0)


def test_g18_arc_center_uses_k_and_i():
    text = "G0 X0 Y0 Z0\nG18 G2 X0.317 Z-0.317 I0.317 K0 F100"
    arc = [e for e in interpret(parse(text)) if isinstance(e, Motion)][1]
    assert arc.plane == "zx" and arc.center == pytest.approx((0.317, 0, 0))
