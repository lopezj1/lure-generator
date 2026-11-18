import dataclasses
import shutil
from pathlib import Path

import pytest

from luregen.fab.printer import orca
from luregen.fab.printer.machine import load_machine
from luregen.fab.printer.orca import (
    SlicerError,
    build_command,
    choose_runtime,
    docker_prefix,
    find_slicer,
    format_override,
    slice_stl,
)

REPO = Path(__file__).resolve().parents[2]
U1 = REPO / "spec" / "printers" / "snapmaker_u1"

# Stands in for orca-slicer: writes plate_1.gcode and result.json into --outputdir.
FAKE_SLICER = """#!/bin/sh
out=""
prev=""
for a in "$@"; do
  [ "$prev" = "--outputdir" ] && out="$a"
  prev="$a"
done
echo "args: $*"
printf ';LAYER_CHANGE\\n;Z:0.2\\nG1 X1 E1\\n' > "$out/plate_1.gcode"
echo '{"return_code": 0, "error_string": "Success."}' > "$out/result.json"
"""


@pytest.fixture
def u1(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("LUREGEN_SLICER", raising=False)
    monkeypatch.delenv("LUREGEN_SLICER_RUNTIME", raising=False)
    return dataclasses.replace(load_machine(U1), cache_dir=tmp_path / "cache")


def test_format_override():
    assert format_override("enable-prime-tower", False) == "--enable-prime-tower=0"
    assert format_override("wall-loops", 4) == "--wall-loops=4"
    assert format_override("ironing-type", "no ironing") == "--ironing-type=no ironing"


def test_build_command(u1):
    profiles = u1.profile_paths()
    cmd = build_command(Path("/opt/orca"), profiles, u1.overrides, Path("/w/head.stl"), Path("/o"))
    assert cmd[:3] == ["xvfb-run", "-a", "/opt/orca"]
    assert cmd[3:5] == ["--slice", "0"]
    assert cmd[cmd.index("--load-settings") + 1] == f"{profiles['machine']};{profiles['process']}"
    assert cmd[cmd.index("--load-filaments") + 1] == str(profiles["filament"])
    assert "--enable-prime-tower=0" in cmd
    assert "--sparse-infill-density=100%" in cmd
    assert "--seam-position=back" in cmd
    assert cmd[-3:] == ["--outputdir", "/o", "/w/head.stl"]
    bare = build_command(Path("/opt/orca"), profiles, {}, Path("/w/a.stl"), Path("/o"), xvfb=False)
    assert bare[0] == "/opt/orca"


def test_missing_binary_gives_clear_error(u1, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(SlicerError, match=r"OrcaSlicer 2\.5\.1 not found.*AppImage"):
        find_slicer(u1, u1.slicer(), fetch=False)
    monkeypatch.setenv("LUREGEN_SLICER", "/nonexistent/orca")
    with pytest.raises(SlicerError, match="LUREGEN_SLICER"):
        find_slicer(u1, u1.slicer(), fetch=False)


def test_cached_apprun_is_found(u1, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    apprun = u1.cache_dir / "orca-2.5.1" / "squashfs-root" / "AppRun"
    apprun.parent.mkdir(parents=True)
    apprun.write_text("#!/bin/sh\n")
    assert find_slicer(u1, u1.slicer(), fetch=False) == apprun


def test_choose_runtime(u1, monkeypatch):
    app = u1.slicer()
    monkeypatch.setattr(orca, "host_glibc", lambda: "2.39")
    assert choose_runtime(u1, app) == "host"
    monkeypatch.setattr(orca, "host_glibc", lambda: "2.36")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    assert choose_runtime(u1, app) == "docker"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(SlicerError, match="glibc >= 2.38"):
        choose_runtime(u1, app)
    assert choose_runtime(u1, app, Path("/usr/bin/orca-slicer")) == "host"  # native install
    monkeypatch.setenv("LUREGEN_SLICER_RUNTIME", "host")
    assert choose_runtime(u1, app) == "host"


def test_docker_prefix(tmp_path: Path):
    cmd = docker_prefix("img:1", [tmp_path, tmp_path / "."], tmp_path)
    assert cmd[:4] == ["docker", "run", "--rm", "--init"]
    assert cmd.count(f"{tmp_path}:{tmp_path}") == 1
    assert cmd[-1] == "img:1"


def test_slice_rejects_non_stl(u1, tmp_path: Path):
    model = tmp_path / "head.3mf"
    model.write_text("")
    with pytest.raises(SlicerError, match="feed STL"):
        slice_stl(u1, model, tmp_path / "out")


def test_slice_with_fake_slicer(u1, tmp_path: Path, monkeypatch):
    fake = tmp_path / "fake-orca"
    fake.write_text(FAKE_SLICER)
    fake.chmod(0o755)
    monkeypatch.setenv("LUREGEN_SLICER", str(fake))
    monkeypatch.setenv("LUREGEN_SLICER_RUNTIME", "host")
    stl = tmp_path / "head.stl"
    stl.write_text("solid x\nendsolid x\n")
    result = slice_stl(u1, stl, tmp_path / "out")
    assert result.ok, result.log.read_text()
    assert result.gcode == tmp_path / "out" / "head.gcode"
    assert result.result["error_string"] == "Success."
    log = result.log.read_text()
    assert "--enable-prime-tower=0" in log
    assert not list((tmp_path / "out").glob(".slice-*"))


@pytest.mark.integration
def test_real_slice_of_a_cube(tmp_path: Path):
    """Downloads the pinned AppImage on first run (~200 MB) and slices a 20 mm cube."""
    import trimesh

    from luregen.fab.printer.verify import parse_gcode

    cube = trimesh.creation.box(extents=(20, 20, 20))
    cube.apply_translation((0, 0, 10))
    stl = tmp_path / "cube.stl"
    cube.export(stl)
    result = slice_stl(load_machine(U1), stl, tmp_path / "out")
    assert result.ok, result.log.read_text()[-2000:]
    summary = parse_gcode(result.gcode)
    assert summary.layer_count == 100
    assert summary.config["enable_prime_tower"] == "0"
    assert summary.filament_g == pytest.approx(20**3 / 1000 * 1.27, rel=0.05)
