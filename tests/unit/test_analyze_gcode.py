from pathlib import Path

from router_jobs import GOLDEN, REPO, ROUTER
from typer.testing import CliRunner

from luregen.cli import make_app
from luregen.fab.router.analyze import analyze, diff, propose
from luregen.fab.router.post_config import load_post

CARBIDE = REPO / "tests" / "fixtures" / "carbide_style.nc"
FUSION = ROUTER / "reference" / "fusion_sample_excerpt.nc"


def test_carbide_style_fixture():
    a = analyze(CARBIDE.read_text())
    assert a.units == "mm" and a.word_separator == ""
    assert a.decimals["X"] == 3 and a.decimals["F"] == 1 and a.decimals["S"] == 0
    assert not a.trim_zeros and a.arcs == 0
    assert a.header[:4] == [
        "(Design File: {design})",
        "(stockMin:{stock_min})",
        "(stockMax:{stock_max})",
        "(STOCK/BLOCK,{stock_block})",
    ]
    assert a.header[-1] == "G53 G0 Z-5.000" and "(Contour.Toolpath.1)" not in a.header
    assert a.tool_change_style == "M6"
    assert a.tool_change == [
        "M05",
        "(TOOL/MILL,{dia}, {corner_r}, 0.00, 0.00)",
        "M6 T{tool_number}",
    ]
    assert a.spindle_on == ["M03 S{rpm}"] and a.spindle_dwell_s == 0
    assert a.retract_style == "G53" and a.footer == ["M02"] and a.end_code == "M2"


def test_fusion_style_reference():
    a = analyze(FUSION.read_text())
    assert a.word_separator == " " and a.trim_zeros
    assert a.arcs > 0 and a.arc_planes == ["xy", "zx"] and a.arc_format == "ij"
    assert a.min_arc_radius is not None and 0.3 < a.min_arc_radius < 0.33
    assert a.tool_change_style == "T only" and a.tool_change == ["T{tool_number}"]
    assert a.spindle_on == ["S{rpm} M3"]
    assert a.retract_style == "G28" and a.retract == "G28 G91 Z0"
    assert a.footer[-2:] == ["M5", "M30"] and a.end_code == "M30"
    assert all("D=" not in h for h in a.header)


def test_own_post_output_round_trips_to_current_config():
    current = load_post(ROUTER / "post.toml")
    proposed = propose(analyze((GOLDEN / "two_tool.nc").read_text()), current)
    assert proposed.word_separator == current.word_separator
    assert proposed.decimals == current.decimals
    assert proposed.tool_change == ["M05", "G53 G0 Z-5.000", *current.tool_change[2:]]
    assert proposed.spindle.on == current.spindle.on
    assert proposed.spindle.dwell_s == current.spindle.dwell_s


def test_path_comments_are_dropped(tmp_path: Path):
    text = "(Design File: C:\\Users\\someone\\part.c2d)\n(made at C:\\temp)\nG90\nG21\nG0 X1\nM30\n"
    a = analyze(text)
    assert a.header == ["(Design File: {design})", "G90", "G21"]


def test_diff_lists_changes():
    current = load_post(ROUTER / "post.toml")
    d = diff(current, propose(analyze(FUSION.read_text()), current))
    assert '-word_separator = ""' in d and '+word_separator = " "' in d


def test_cli_prints_proposal_and_diff(tmp_path: Path):
    out = tmp_path / "post.toml"
    result = CliRunner().invoke(
        make_app(),
        ["analyze-gcode", str(CARBIDE), "--spec", str(REPO / "spec"), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "Proposed post.toml" in result.output and "Diff against" in result.output
    assert load_post(out).word_separator == ""
