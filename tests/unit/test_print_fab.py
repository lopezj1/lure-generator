"""`lure fab --format print` (#35, #51): orientation, plan, thumbnails, pause, job wiring.

CI has no slicer: the job runs against a fake `orca-slicer` that writes Orca-style G-code.
The real end-to-end slice is an integration test (container runtime OK).
"""

import dataclasses
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import trimesh
from print_jobs import install_fake_slicer

from luregen.config import load_config
from luregen.fab.printer import job, thumbnails
from luregen.fab.printer.machine import FilamentSlot, MachineConfigError, load_machine
from luregen.fab.printer.orca import build_command
from luregen.fab.printer.orient import bed_center, orient_mesh, upright_transform
from luregen.fab.printer.plan import PlanError, plan_job, supports_needed
from luregen.fab.printer.verify import count_tool_changes, parse_gcode
from luregen.report.build_dir import create_build_dir

REPO = Path(__file__).resolve().parents[2]
U1 = REPO / "spec" / "printers" / "snapmaker_u1"
SAMPLE = REPO / "spec" / "designs" / "glide" / "variants" / "striper_175_petg.toml"


@pytest.fixture(scope="module")
def u1():
    return load_machine(U1)


def hollow_box(lo=(0, -8, -10), hi=(40, 8, 12), wall=2.0):
    """A closed box with a sealed box void: outer skin + inverted inner shell."""
    lo, hi = np.array(lo, float), np.array(hi, float)
    outer = trimesh.creation.box(bounds=[lo, hi])
    inner = trimesh.creation.box(bounds=[lo + wall, hi - wall])
    inner.invert()
    return trimesh.util.concatenate([outer, inner])


# --- machine / profiles -----------------------------------------------------------------


def test_machine_slots_and_profiles(u1):
    assert [s.tool for s in u1.filaments] == ["T0", "T1", "T2", "T3"]
    assert u1.slot(1).material == "PETG"
    assert u1.interface_pairs["PETG"] == "PLA"
    assert u1.filament_profile("PETG").name == "filament.json"
    assert u1.filament_profile("PLA").name == "filament-PLA.json"
    pla = json.loads(u1.filament_profile("PLA").read_text())
    assert pla["filament_type"] == ["PLA"] and "inherits" not in pla
    with pytest.raises(MachineConfigError, match="no ASA filament"):
        u1.filament_profile("ASA")
    with pytest.raises(MachineConfigError, match="no filament slot T7"):
        u1.slot(7)


# --- plan ---------------------------------------------------------------------------------


def test_plan_loft3d_dissimilar_interface(u1):
    cfg = load_config(SAMPLE)  # PETG loft3d; variant picks T1 body, T0 interface
    plan = plan_job(cfg, u1)
    assert (plan.body.tool, plan.interface.tool) == ("T1", "T0")
    assert plan.supports and plan.multi_filament
    o = plan.overrides
    assert o["enable-prime-tower"] is True
    assert o["enable-support"] is True and o["support-type"] == "tree(auto)"
    assert o["support-top-z-distance"] == 0 and o["support-interface-top-layers"] == 3
    assert o["support-filament"] == 2 and o["support-interface-filament"] == 1
    assert o["support-on-build-plate-only"] is True
    assert plan.filament_ids == "2"
    assert [p.name for p in plan.filaments] == [
        "filament-PLA.json",
        "filament.json",
        "filament-PLA.json",
        "filament-TPU.json",  # T3 holds TPU for the separate tail fin (#123)
    ]
    assert not plan.warnings


def test_plan_defaults_pick_slots_by_material(u1):
    cfg = load_config(SAMPLE)
    cfg.print.body_slot = None
    cfg.print.interface_slot = None
    plan = plan_job(cfg, u1)
    assert (plan.body.slot, plan.interface.slot) == (1, 0)


def test_plan_single_filament_keeps_prime_tower_off(u1):
    cfg = load_config(SAMPLE, ["print.interface_slot=1"])  # interface = body slot
    plan = plan_job(cfg, u1)
    assert plan.supports and not plan.multi_filament and plan.interface is None
    assert plan.overrides["enable-prime-tower"] is False
    assert plan.overrides["support-interface-filament"] == 2
    assert "support-top-z-distance" not in plan.overrides  # stock gap without a dissimilar pair


def test_plan_warnings_and_errors(u1):
    cfg = load_config(SAMPLE, ["print.interface_slot=2"])
    same = dataclasses.replace(
        u1, filaments=(FilamentSlot(0, "PETG"), FilamentSlot(1, "PETG"), FilamentSlot(2, "PETG"))
    )
    plan = plan_job(cfg, same)
    assert any("same material" in w for w in plan.warnings)
    cfg = load_config(SAMPLE)
    cfg.print.body_slot = cfg.print.interface_slot = None
    lonely = dataclasses.replace(u1, filaments=(FilamentSlot(0, "PETG"),))
    plan = plan_job(cfg, lonely)
    assert plan.interface is None and any("dissimilar" in w for w in plan.warnings)
    with pytest.raises(PlanError, match="no filament slot holds PETG"):
        plan_job(cfg, dataclasses.replace(u1, filaments=(FilamentSlot(0, "PLA"),)))
    gap = dataclasses.replace(u1, filaments=(FilamentSlot(0, "PETG"), FilamentSlot(2, "PLA")))
    with pytest.raises(PlanError, match="T0..T1"):
        plan_job(cfg, gap)


def test_plan_supports_off_and_user_overrides(u1):
    cfg = load_config(SAMPLE, ["print.supports=off", "print.overrides.wall-loops=5"])
    plan = plan_job(cfg, u1)
    assert not plan.supports and plan.interface is None
    assert plan.overrides["enable-support"] is False
    assert plan.overrides["enable-prime-tower"] is False
    assert plan.overrides["wall-loops"] == 5


def test_supports_needed_profile25d_by_roundover():
    cfg = load_config(SAMPLE, ["process.body_style=profile25d"])
    assert supports_needed(cfg, 4.0)[0]
    assert not supports_needed(cfg, 1.5)[0]
    assert not supports_needed(cfg, None)[0]


def test_build_command_with_slot_filaments(tmp_path: Path):
    profiles = {k: tmp_path / f"{k}.json" for k in ("machine", "process", "filament")}
    fils = [tmp_path / "a.json", tmp_path / "b.json"]
    cmd = build_command(Path("orca"), profiles, {}, tmp_path / "x.stl", tmp_path, False, fils, "2")
    i = cmd.index("--load-filaments")
    assert cmd[i + 1] == f"{fils[0]};{fils[1]}"
    assert cmd[cmd.index("--load-filament-ids") + 1] == "2"


# --- orientation ------------------------------------------------------------------------


def test_orient_upright_translates_only():
    mesh = hollow_box()
    sec = orient_mesh("head", mesh, (135.5, 136.0))
    assert sec.shells == 2
    assert np.allclose(sec.transform[:3, :3], np.eye(3))  # z up as modeled
    assert sec.mesh.bounds[0][2] == pytest.approx(0.0)
    assert sec.print_z(8.0) == pytest.approx(18.0)  # lure z -10 → 0
    center = sec.mesh.bounds.mean(axis=0)
    assert center[:2] == pytest.approx([135.5, 136.0])
    assert sec.mesh.volume == pytest.approx(mesh.volume)
    t = upright_transform(np.array([[0, 0, -3.0], [10, 4, 5]]), (0, 0))
    assert t[2, 3] == 3.0


def test_orient_rejects_open_mesh():
    mesh = trimesh.creation.box()
    mesh.update_faces(np.arange(len(mesh.faces) - 1))
    with pytest.raises(ValueError, match="not closed"):
        orient_mesh("tail", mesh, (0, 0))


def test_bed_center(u1):
    profile = json.loads(u1.profile_paths()["machine"].read_text())
    assert bed_center(profile) == pytest.approx((135.5, 136.0))


# --- thumbnails ---------------------------------------------------------------------------


def test_render_and_inject_thumbnails():
    mesh = hollow_box()
    img = thumbnails.render(mesh.vertices, mesh.faces, 48, 48)
    assert img.shape == (48, 48, 4)
    assert 0.2 < (img[..., 3] > 0).mean() < 0.95  # part drawn, background transparent
    thumbs = thumbnails.render_thumbnails(mesh, [(48, 48), (300, 300)], "#3C78D8")
    gcode = "; HEADER_BLOCK_START\n; x\n; HEADER_BLOCK_END\n\nG28\n"
    once = thumbnails.inject_thumbnails(gcode, thumbs)
    assert thumbnails.inject_thumbnails(once, thumbs) == once  # idempotent
    assert once.index("; HEADER_BLOCK_END") < once.index("; THUMBNAIL_BLOCK_START")
    assert once.index("; THUMBNAIL_BLOCK_END") < once.index("G28")
    assert all(len(line) <= 80 for line in once.splitlines())
    decoded = thumbnails.decode_thumbnails(once)
    assert decoded == {"48x48": thumbs[0].png, "300x300": thumbs[1].png}
    assert decoded["48x48"].startswith(b"\x89PNG")


def test_parse_sizes():
    assert thumbnails.parse_sizes("48x48/PNG, 300x300/PNG") == [(48, 48), (300, 300)]
    assert thumbnails.parse_sizes("96x96/JPG") == [(48, 48), (300, 300)]
    assert thumbnails.parse_sizes(None) == [(48, 48), (300, 300)]


# --- G-code stats ---------------------------------------------------------------------------


def test_count_tool_changes():
    body = ["T1", "G1 X1", "T1", "T0 ; interface", "G1 E1", "T1", "TIMELAPSE_STOP"]
    assert count_tool_changes(body) == (2, [1, 0])


def test_per_slot_grams(tmp_path: Path):
    p = tmp_path / "a.gcode"
    p.write_text(
        "; filament used [mm] = 767.39, 10717.88, 0.00, 0.00\n"
        "; filament used [g] = 2.29, 32.74, 0.00, 0.00\n"
        "; total filament used [g] = 35.03\n"
    )
    s = parse_gcode(p)
    assert s.filament_g_by_slot == [2.29, 32.74, 0.0, 0.0]
    assert s.filament_g == pytest.approx(35.03)


# --- the job, with a fake slicer -----------------------------------------------------------


@pytest.fixture
def print_build(tmp_path: Path) -> Path:
    """A print build folder with box sections (sealed voids) and a cavity ceiling in report."""
    cfg = load_config(SAMPLE)
    build = create_build_dir(cfg, tmp_path / "builds")
    (build / "model").mkdir()
    for name, x0 in (("head", 0), ("tail", 45)):
        hollow_box(lo=(x0, -8, -10), hi=(x0 + 40, 8, 12)).export(build / "model" / f"{name}.stl")
    report = json.loads((build / "report.json").read_text())
    report["status"] = "built"
    report["sections"] = {"head": {}, "tail": {}}
    report["geometry"] = {
        "internals": {
            "head": {"cavity_ceiling_z_mm": 6.0},  # print Z 16.0 → layer 80
            "tail": {"cavity_ceiling_z_mm": 6.1},  # print Z 16.1 → layer 81 (Z 16.2)
        }
    }
    (build / "report.json").write_text(json.dumps(report))
    return build


@pytest.fixture
def fake_slicer(tmp_path: Path, monkeypatch) -> Path:
    return install_fake_slicer(tmp_path, monkeypatch)


def test_job_with_fake_slicer(print_build: Path, fake_slicer: Path, monkeypatch):
    events = []
    monkeypatch.setattr(job.progress, "emit", lambda *a: events.append(a))
    res = job.run(print_build, REPO / "spec")
    assert res.ok, [s.issues for s in res.sections]
    out = print_build / "fab" / "print"
    assert res.out_dir == out
    assert res.files == ["fab/print/head.gcode", "fab/print/tail.gcode"]
    head, tail = res.sections
    assert head.pause["layer"] == 80 and head.pause["layer_z_mm"] == pytest.approx(16.0)
    assert tail.pause["layer"] == 81 and tail.pause["requested_z_mm"] == pytest.approx(16.1)
    text = (out / "head.gcode").read_text()
    assert text.count("M600") == 1
    assert text.index("; LUREGEN_PAUSE layer=79") < text.index(";Z:16.0")
    assert set(thumbnails.decode_thumbnails(text)) == {"48x48", "300x300"}
    st = head.stats
    assert (st["tool_changes"], st["tools_used"]) == (20, ["T1", "T0"])
    assert (st["interface_g"], st["body_g"], st["layers"]) == (0.3, 2.7, 300)
    assert st["time_s"] == 750
    assert head.verify["references"]  # diffed against spec/.../reference/
    payload = json.loads((out / "print.json").read_text())
    assert payload["totals"]["interface_g"] == pytest.approx(0.6)
    assert payload["totals"]["tool_changes"] == 40
    assert payload["plan"]["prime_tower"] is True
    md = (out / "print.md").read_text()
    assert "| head | T1 PETG | 12m 30s |" in md and "T0 PLA" in md
    log = (out / "head.slice.log").read_text()
    assert "--load-filament-ids 2" in log and "--enable-prime-tower=1" in log
    assert "filament-PLA.json" in log and "--support-top-z-distance=0" in log
    assert (out / "head.stl").is_file()
    assert {e[0] for e in events} >= {"plan", "orient", "slice", "post", "verify", "report"}
    fracs = [e[1] for e in events]
    assert fracs == sorted(fracs)


def test_job_rejects_cnc_and_unbuilt(print_build: Path, tmp_path: Path):
    text = (print_build / "resolved.toml").read_text().replace('method = "print"', 'method = "cnc"')
    cnc = tmp_path / "cnc"
    shutil.copytree(print_build, cnc, symlinks=True)
    (cnc / "resolved.toml").write_text(text)
    with pytest.raises(job.PrintJobError, match="cnc build"):
        job.run(cnc, REPO / "spec")
    report = json.loads((print_build / "report.json").read_text())
    report["status"], report["sections"] = "config_only", {}
    (print_build / "report.json").write_text(json.dumps(report))
    with pytest.raises(job.PrintJobError, match="no model"):
        job.run(print_build, REPO / "spec")


def test_cli_fab_print(print_build: Path, fake_slicer: Path, monkeypatch):
    from typer.testing import CliRunner

    from luregen.cli import make_app

    monkeypatch.chdir(REPO)
    res = CliRunner().invoke(make_app(), ["fab", str(print_build), "--format", "print"])
    assert res.exit_code == 0, res.output
    assert "body T1 PETG; supports on; interface T0 PLA" in res.output
    assert "fab/print/head.gcode" in res.output and "pause layer 80" in res.output


@pytest.mark.integration
@pytest.mark.slow
def test_real_print_fab_of_the_sample(tmp_path: Path):
    """Full build + real slice (container runtime OK): PETG body, PLA interface, prime tower."""
    from typer.testing import CliRunner

    from luregen.cli import make_app

    app = make_app()
    res = CliRunner().invoke(app, ["build", str(SAMPLE), "--out-root", str(tmp_path)])
    assert res.exit_code == 0, res.output
    build = tmp_path / "glide" / "striper_175_petg" / "latest"
    result = job.run(build.resolve(), REPO / "spec")
    assert result.ok, [s.issues for s in result.sections]
    for s in result.sections:
        assert s.stats["tool_changes"] > 0 and s.stats["interface_g"] > 0
        assert s.pause is not None
        summary = parse_gcode(build / s.gcode)
        assert summary.config["enable_prime_tower"] == "1"
        assert {f"{t.width}x{t.height}" for t in summary.thumbnails} == {"48x48", "300x300"}
