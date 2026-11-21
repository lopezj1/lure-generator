"""Separate TPU tail fin (#123): config, geometry, scaling, balance, DFM and fab."""

import json
from pathlib import Path

import pytest

from luregen.config import dump_resolved, load_config, load_resolved
from luregen.physics.scaling import length_for_weight, scale_config
from luregen.profile.table import StationTable

REPO = Path(__file__).resolve().parents[2]
VARIANT = REPO / "spec/designs/glide/variants/glide_2oz_tpu_fin.toml"
STRIPER = REPO / "spec/designs/glide/variants/striper_175_petg.toml"
PRESET = REPO / "spec/designs/glide/profiles/presets/placeholder_175.csv"


@pytest.fixture(scope="module")
def table() -> StationTable:
    return StationTable.load_csv(PRESET)


@pytest.fixture(scope="module")
def cfg():
    return load_config(VARIANT)


# --- config -------------------------------------------------------------------------------


def test_variant_resolves_fin_material_and_screw(cfg):
    assert cfg.fin is not None and cfg.fin.material == "TPU"
    assert cfg.fin.density == pytest.approx(1.21)
    sc = cfg.fin.screw
    assert sc.id == "screw_m2x6_pan" and sc.material == "stainless"
    assert sc.mass_g == (0.15, 0.25) and sc.density == pytest.approx(8.0)
    assert sc.measured is False
    assert cfg.size.target_weight_oz == 2.0
    assert cfg.is_resolved
    assert load_config(STRIPER).fin is None  # other variants are unchanged


def test_resolved_round_trip(cfg, tmp_path: Path):
    path = tmp_path / "resolved.toml"
    path.write_text(dump_resolved(cfg))
    again = load_resolved(path)
    assert again == cfg
    assert again.fin.screw.mass_g == cfg.fin.screw.mass_g


def test_fin_lengths_scale_but_thickness_and_screw_do_not(cfg):
    out = scale_config(cfg, 175.0, 140.0)
    f, f0 = out.fin, cfg.fin
    assert f.cut_from_tail_mm == pytest.approx(f0.cut_from_tail_mm * 0.8)
    assert f.length_mm == pytest.approx(f0.length_mm * 0.8)
    assert f.span_mm == pytest.approx(f0.span_mm * 0.8)
    assert f.thickness_mm == f0.thickness_mm
    assert f.slot == f0.slot and f.screw.length_mm == f0.screw.length_mm


def test_length_for_weight_with_area_and_constant_terms():
    v0, a0, c = 70_000.0, 2_000.0, 600.0
    length = length_for_weight(175.0, v0, 60.0, 1.045, a0, c)
    s = length / 175.0
    assert 1.045 * (v0 * s**3 + a0 * s**2 + c) / 1000.0 == pytest.approx(60.0, rel=1e-9)
    assert length_for_weight(175.0, v0, 70.0, 1.0) == pytest.approx(175.0)


# --- geometry -----------------------------------------------------------------------------


def test_fin_tab_slot_and_screw_geometry(cfg, table):
    from luregen.geometry import fin as fn
    from luregen.geometry.envelope import build_envelope
    from luregen.geometry.validate import assert_valid

    spec = fn.fin_spec(cfg, table)
    fn.check_spec(spec, x_min=90.0)
    assert spec.x_cut == pytest.approx(175.0 - cfg.fin.cut_from_tail_mm)
    assert spec.slot_width == pytest.approx(cfg.fin.thickness_mm + cfg.fin.slot.clearance_mm)
    assert spec.tab_x0 < spec.screw_x < spec.x_cut

    fin = fn.fin_solid(spec)
    assert_valid(fin, solids=1, shells=1, what="fin")
    bb = fin.bounding_box()
    assert pytest.approx(cfg.fin.thickness_mm, abs=1e-3) == bb.size.Y
    assert pytest.approx(spec.tab_x0, abs=1e-3) == bb.min.X  # the tab reaches into the slot
    assert pytest.approx(spec.x_tip, abs=1e-3) == bb.max.X

    env = build_envelope(table, style="loft3d", section_shape="ellipse", roundover_r=4.0).solid
    tail = fn.cut_tail_end(env, spec)
    assert_valid(tail, solids=1, shells=1, what="tail end")
    assert pytest.approx(spec.x_cut, abs=1e-3) == tail.bounding_box().max.X
    # the tab sits in the slot without touching the tail (clearance all round)
    overlap = tail & fn.fin_body(spec)
    assert overlap is None or overlap.volume < 1e-3
    tip, paddle = fn.volumes(spec, env)
    assert tip > 0 and paddle == pytest.approx(fin.volume, rel=0.2)

    walls = fn.slot_walls(spec, table, "ellipse")
    assert walls["side_mm"] > 1.6 and walls["vertical_mm"] > 1.6
    assert 6.0 < fn.local_width(spec, table, "ellipse") < 10.0


def test_fork_and_limits(cfg, table):
    from luregen.geometry import fin as fn

    forked = cfg.model_copy(deep=True)
    forked.fin.fork_depth_mm = 8.0
    spec = fn.fin_spec(forked, table)
    assert fn.fin_solid(spec).volume < fn.fin_solid(fn.fin_spec(cfg, table)).volume

    deep = cfg.model_copy(deep=True)
    deep.fin.slot.depth_mm = 40.0
    with pytest.raises(fn.FinError, match="reaches the joint"):
        fn.check_spec(fn.fin_spec(deep, table), x_min=130.0)
    tall = cfg.model_copy(deep=True)
    tall.fin.root_height_mm = 40.0
    with pytest.raises(fn.FinError, match="root height"):
        fn.check_spec(fn.fin_spec(tall, table), x_min=90.0)


# --- full build (balance, report, DFM, fab) -------------------------------------------------


@pytest.fixture(scope="module")
def fin_build(tmp_path_factory) -> Path:
    """The new default variant built with the solver off (fast; the solve is the e2e test)."""
    from typer.testing import CliRunner

    from luregen.cli import make_app

    root = tmp_path_factory.mktemp("builds")
    res = CliRunner().invoke(
        make_app(),
        ["build", str(VARIANT), "--out-root", str(root), "--set", "solver.enabled=false"],
    )
    assert res.exit_code in (0, 1), res.output  # DFM may fail an unsolved build
    return (root / "glide" / "glide_2oz_tpu_fin" / "latest").resolve()


@pytest.mark.slow
def test_build_exports_fin_and_counts_it_in_the_tail(fin_build: Path):
    report = json.loads((fin_build / "report.json").read_text())
    fin = report["geometry"]["fin"]
    assert (fin_build / "model" / "fin.stl").is_file()
    assert fin["files"]["stl"]["path"] == "model/fin.stl" and fin["files"]["stl"]["watertight"]
    assert fin["material"] == "TPU" and fin["mass_g"] == pytest.approx(
        1.21 * fin["volume_cm3"], rel=1e-3
    )
    assert set(report["sections"]) == {"head", "tail"}  # the fin is a part, not a section
    # the tail's mass includes the fin and its screw: tail CG is pulled aft of a fin-less tail
    tail = report["sections"]["tail"]
    assert tail["mass_g"] > fin["mass_g"] + 0.15
    scaling = report["balance"]["scaling"]
    assert scaling["volume_terms_cm3"]["const"] > 0
    md = (fin_build / "report.md").read_text()
    assert "## Tail fin" in md and "M2 × 6 mm" in md
    ids = {v["rule"] for v in report["dfm"]["violations"]}
    assert not ids & {"fin.slot_walls", "fin.screw_clearance"}
    assert {"fin.slot_walls", "fin.screw_clearance", "fin.screw_length"} <= set(
        report["dfm"]["checked"]
    )


@pytest.mark.slow
def test_fin_mass_inputs_use_tpu_density(cfg, table):
    from luregen.commands.build import build_envelope
    from luregen.families import get_family

    cfg = cfg.model_copy(deep=True)
    cfg.size.target_weight_oz, cfg.size.length_mm = None, 175.0
    built = get_family("glide").build(cfg, table, build_envelope(cfg, table), lambda *a: None)
    tail = next(s for s in built.mass_inputs if s.name == "tail")
    fin = next(p for p in tail.parts if p.name == "fin")
    assert fin.density == pytest.approx(1.21)
    assert fin.volume_mm3 == pytest.approx(built.parts["fin"].volume)
    assert any("M2" in h.name for h in tail.hardware)
    # the cavity may wrap above and below the slot, but stays inside the cut end
    cavity = built.report["internals"]["tail"]
    assert cavity["cavity_x_mm"][1] < built.report["fin"]["x_cut_mm"]
    assert cavity["min_skin_mm"] >= cfg.dfm.print.min_skin_to_cavity_mm


@pytest.mark.slow
def test_print_fab_slices_the_fin_in_tpu_flat(fin_build: Path, tmp_path: Path, monkeypatch):
    from print_jobs import install_fake_slicer

    from luregen.fab.printer import job

    install_fake_slicer(tmp_path, monkeypatch)
    res = job.run(fin_build, REPO / "spec", parts=["fin"])
    assert [s.name for s in res.sections] == ["fin"]
    fin = res.sections[0]
    assert fin.gcode == "fab/print/fin.gcode" and fin.material == "TPU" and fin.tool == "T3"
    assert fin.orient["orientation"] == "flat" and fin.pause is None
    assert fin.orient["height_mm"] == pytest.approx(2.0, abs=0.05)  # lying on its side
    log = (fin_build / "fab" / "print" / "fin.slice.log").read_text()
    assert "--load-filament-ids 4" in log and "--enable-support=0" in log
    assert "filament-TPU.json" in log
    tpu = json.loads(
        (REPO / "spec/printers/snapmaker_u1/profiles/orca/filament-TPU.json").read_text()
    )
    assert tpu["name"] == "Generic TPU @U1 0.4 nozzle" and tpu["filament_type"] == ["TPU"]
    with pytest.raises(job.PrintJobError, match="cannot print"):
        job.run(fin_build, REPO / "spec", parts=["gill"])


def test_cnc_build_prints_only_the_fin(cfg):
    from luregen.fab.printer.job import PrintJobError, printable_parts

    report = {"sections": {"head": {}, "tail": {}}, "geometry": {"fin": {"x_cut_mm": 1}}}
    assert printable_parts(cfg, report) == ["head", "tail", "fin"]
    assert printable_parts(cfg, report, ["fin", "head"]) == ["head", "fin"]
    wood = cfg.model_copy(deep=True)
    wood.process.method = "cnc"
    assert printable_parts(wood, report) == ["fin"]
    with pytest.raises(PrintJobError, match="cannot print head"):
        printable_parts(wood, report, ["head"])
    with pytest.raises(PrintJobError, match="no printed parts"):
        printable_parts(wood, {"sections": {"head": {}}, "geometry": {}})


def test_cnc_glue_face_gets_the_fin_slot_pocket():
    from luregen.fab.router.cam.job import fin_notes, fin_slot_region

    fin = {
        "material": "TPU",
        "mass_g": 1.6,
        "slot": {"x_mm": [130.0, 140.0], "z_mm": [-3.0, 4.0], "corner_r_mm": 1.85},
        "screw": {"pilot_d_mm": 1.6, "x_mm": 135.0, "z_mm": 0.5, "name": "M2 screw"},
    }
    region = fin_slot_region(fin)
    x0, z0, x1, z1 = region.bounds
    assert x0 == pytest.approx(130.0) and x1 > 140.0  # open past the tail end
    assert (z0, z1) == pytest.approx((-3.0, 4.0))
    notes = fin_notes({"geometry": {"fin": fin}})
    assert "--format print" in notes[0] and "⌀1.6 mm" in notes[1]
