from pathlib import Path

import pytest

from luregen.fab.printer.gcode_post import (
    PauseError,
    find_layers,
    insert_pauses,
    insert_pauses_file,
)

FIXTURE = Path(__file__).parent / "fixtures" / "orca_2.5.1_cube.gcode"


@pytest.fixture
def gcode() -> str:
    return FIXTURE.read_text()


def test_find_layers(gcode: str):
    zs = [layer.z for layer in find_layers(gcode.splitlines())]
    assert zs == [0.25, 0.45, 0.65, 0.85, 1.05]


def test_pause_goes_before_first_layer_at_or_above(gcode: str):
    out, pauses = insert_pauses(gcode, [0.5])
    assert [(p.layer.index, p.layer.z, p.inserted) for p in pauses] == [(2, 0.65, True)]
    lines = out.splitlines()
    i = lines.index("M600")
    assert lines[i - 2].startswith("; LUREGEN_PAUSE layer=2 z=0.650 requested_z=0.500")
    assert lines[i + 1] == ";LAYER_CHANGE"
    assert lines[i + 2] == ";Z:0.65"
    assert out.count("\nM600\n") == 1


def test_exact_layer_z_matches_that_layer(gcode: str):
    _, pauses = insert_pauses(gcode, [0.45])
    assert pauses[0].layer.z == 0.45


def test_idempotent(gcode: str):
    once, _ = insert_pauses(gcode, [0.5])
    twice, pauses = insert_pauses(once, [0.5])
    assert twice == once
    assert not pauses[0].inserted


def test_multiple_pauses_and_custom_command(gcode: str):
    out, pauses = insert_pauses(gcode, [1.0, 0.3, 0.31], command="PAUSE\nM117 insert")
    assert [p.layer.index for p in pauses] == [1, 1, 4]
    assert [p.inserted for p in pauses] == [True, False, True]
    assert out.count("\nPAUSE\nM117 insert\n;LAYER_CHANGE") == 2
    # Re-running with one new Z adds only that one.
    again, more = insert_pauses(out, [0.3, 0.7])
    assert again.count("; LUREGEN_PAUSE") == 3
    assert [p.inserted for p in more] == [False, True]


def test_errors(gcode: str):
    with pytest.raises(PauseError, match="no layer at or above"):
        insert_pauses(gcode, [50.0])
    with pytest.raises(PauseError, match="no ;LAYER_CHANGE"):
        insert_pauses("G1 X0\n", [1.0])
    with pytest.raises(PauseError, match="without a ;Z:"):
        insert_pauses(";LAYER_CHANGE\nG1 X0\n", [1.0])


def test_file_in_place(tmp_path: Path, gcode: str):
    path = tmp_path / "head.gcode"
    path.write_text(gcode)
    pauses = insert_pauses_file(path, [0.8])
    assert pauses[0].layer.z == 0.85
    assert path.read_text().count("\nM600\n") == 1
    assert path.read_text().endswith("\n")
