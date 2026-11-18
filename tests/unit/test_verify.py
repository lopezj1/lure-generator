from pathlib import Path

import pytest

from luregen.fab.printer.verify import (
    diff_gcode,
    macros,
    parse_duration,
    parse_gcode,
    verify_against_references,
)

FIXTURE = Path(__file__).parent / "fixtures" / "orca_2.5.1_cube.gcode"


def test_parse_real_orca_output():
    s = parse_gcode(FIXTURE)
    assert (s.generator, s.generator_version) == ("OrcaSlicer", "2.5.1")
    assert s.estimated_time_s == 17 * 60 + 35
    assert s.filament_mm == pytest.approx(3351.46)
    assert s.filament_g == pytest.approx(10.24)
    assert s.filament_cm3 == pytest.approx(8.06)
    assert s.layer_count == 5  # trimmed fixture
    assert s.header_layer_count == 100
    assert s.max_z == pytest.approx(20.05)
    assert s.thumbnails == []  # the Orca CLI writes no thumbnails into plain G-code
    assert s.config["enable_prime_tower"] == "0"
    assert s.config["sparse_infill_density"] == "100%"
    assert s.config["machine_pause_gcode"] == "M600"
    assert s.start_macros[:3] == [
        "SET_PRINT_AUTO_BED_LEVELING",
        "SET_TIME_LAPSE_CAMERA",
        "PRINT_START",
    ]
    assert "BED_MESH_CALIBRATE" in s.start_macros
    assert s.end_macros == ["PRINT_END", "TIMELAPSE_STOP"]
    d = s.to_dict()
    assert d["config"] == len(s.config)
    assert "start_gcode" not in d


def test_thumbnails_and_multi_filament(tmp_path: Path):
    path = tmp_path / "t.gcode"
    path.write_text(
        "; thumbnail begin 48x48 120\n; abc\n; thumbnail end\n"
        "; thumbnail_JPG begin 300x300 4000\n; thumbnail_JPG end\n"
        "; filament used [g] = 1.5, 0.25\n"
        "; estimated printing time (normal mode) = 1d 2h 3m 4s\n"
    )
    s = parse_gcode(path)
    assert [(t.width, t.height, t.format) for t in s.thumbnails] == [
        (48, 48, "PNG"),
        (300, 300, "JPG"),
    ]
    assert s.filament_g == pytest.approx(1.75)
    assert s.estimated_time_s == 86400 + 7200 + 180 + 4


def test_parse_duration_and_macros():
    assert parse_duration("17m 35s") == 1055
    with pytest.raises(ValueError):
        parse_duration("soon")
    lines = ["PRINT_START BED=80", "G28", "M104 S200", "; COMMENT", "T0", "EXCLUDE_OBJECT_END X"]
    assert macros(lines) == ["PRINT_START"]


def test_diff_identical_is_clean():
    s = parse_gcode(FIXTURE)
    assert diff_gcode(s, parse_gcode(FIXTURE)).clean


def test_diff_reports_changes(tmp_path: Path):
    text = FIXTURE.read_text()
    ref = tmp_path / "ref.gcode"
    ref.write_text(
        text.replace("; wall_loops = 4", "; wall_loops = 2")
        .replace("; seam_position = back\n", "")
        .replace("; print_settings_id = 0.20", "; print_settings_id = other 0.20")
        .replace("; CONFIG_BLOCK_END", "; extra_key = 1\n; CONFIG_BLOCK_END")
        .replace("\nTIMELAPSE_STOP\n", "\nM84\n")
        .replace(
            "; HEADER_BLOCK_START",
            "; thumbnail begin 48x48 10\n; thumbnail end\n; HEADER_BLOCK_START",
        )
    )
    d = diff_gcode(parse_gcode(FIXTURE), parse_gcode(ref))
    assert d.changed == {"wall_loops": ("4", "2")}  # print_settings_id is ignored
    assert d.only_ours == ["seam_position"]
    assert d.only_reference == ["extra_key"]
    assert d.end_macros == (["PRINT_END", "TIMELAPSE_STOP"], ["PRINT_END"])
    assert d.start_macros is None
    assert d.thumbnails == ([], ["48x48/PNG"])
    assert not d.to_dict()["clean"]


def test_verify_without_and_with_references(tmp_path: Path):
    report = verify_against_references(FIXTURE, tmp_path)
    assert report.diffs == []
    assert "no reference G-code" in report.note
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "owner.gcode").write_text(FIXTURE.read_text())
    report = verify_against_references(FIXTURE, tmp_path)
    assert report.note is None
    assert [d.clean for d in report.diffs] == [True]
    assert report.to_dict()["summary"]["layer_count"] == 5
