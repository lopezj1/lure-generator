import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from luregen.cli import make_app
from luregen.fab.printer.machine import MachineConfigError, load_machine
from luregen.fab.printer.profiles import (
    ProfileError,
    ProfileSource,
    dir_fetcher,
    flatten_app,
    flatten_profile,
    github_fetcher,
    sync_profiles,
)

REPO = Path(__file__).resolve().parents[2]
U1 = REPO / "spec" / "printers" / "snapmaker_u1"


def _write(path: Path, data: dict, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode())


@pytest.fixture
def profile_tree(tmp_path: Path) -> Path:
    """A tiny resources/profiles/ tree: vendor V and library Lib, with BOMs and an index."""
    root = tmp_path / "profiles"
    _write(
        root / "V.json",
        {
            "machine_list": [
                {"name": "m_base", "sub_path": "machine/m_base.json"},
                {"name": "Printer X", "sub_path": "machine/Printer X.json"},
            ],
            "process_list": [{"name": "0.20 Std @X", "sub_path": "process/0.20 Std @X.json"}],
            "filament_list": [{"name": "PETG @X", "sub_path": "filament/PETG @X.json"}],
        },
    )
    _write(
        root / "Lib.json",
        {
            "filament_list": [
                {"name": "fil_base", "sub_path": "filament/base/fil_base.json"},
                {"name": "PETG @System", "sub_path": "filament/PETG @System.json"},
            ]
        },
    )
    m = root / "V" / "machine"
    _write(
        m / "m_base.json",
        {
            "type": "machine",
            "name": "m_base",
            "gcode_flavor": "marlin",
            "machine_pause_gcode": "M601",
            "printer_settings_id": "",
            "instantiation": "false",
        },
    )
    _write(
        m / "Printer X.json",
        {
            "type": "machine",
            "name": "Printer X",
            "inherits": "m_base",
            "from": "system",
            "gcode_flavor": "klipper",
            "instantiation": "true",
        },
        bom=True,
    )
    p = root / "V" / "process"
    # Not in the index: found by its conventional file name.
    _write(
        p / "p_common.json",
        {
            "type": "process",
            "name": "p_common",
            "layer_height": "0.3",
            "enable_prime_tower": "1",
            "wipe_tower_filament": "0",
        },
    )
    _write(
        p / "0.20 Std @X.json",
        {"type": "process", "name": "0.20 Std @X", "inherits": "p_common", "layer_height": "0.2"},
    )
    f = root / "V" / "filament"
    _write(
        f / "PETG @X.json",
        {
            "type": "filament",
            "name": "PETG @X",
            "inherits": "PETG @System",
            "nozzle_temperature": ["250"],
        },
        bom=True,
    )
    lib = root / "Lib" / "filament"
    _write(
        lib / "PETG @System.json",
        {
            "type": "filament",
            "name": "PETG @System",
            "inherits": "fil_base",
            "filament_type": ["PETG"],
        },
    )
    _write(
        lib / "base" / "fil_base.json",
        {
            "type": "filament",
            "name": "fil_base",
            "filament_type": ["PLA"],
            "nozzle_temperature": ["200"],
            "filament_settings_id": [""],
        },
    )
    return root


@pytest.fixture
def mini_printer(tmp_path: Path) -> Path:
    root = tmp_path / "printers" / "x"
    root.mkdir(parents=True)
    (root / "machine.toml").write_text(
        'name = "X"\napp = "a"\ncache_dir = "~/.cache/x"\npause_command = "M600"\n'
        "[apps.a]\n"
        'label = "A"\nversion = "1.0"\nrepo = "o/r"\ntag = "v1.0"\nbinary = "a-bin"\n'
        'appimage_url = "https://example.invalid/A_V1.0.AppImage"\n'
        'drop_keys = ["wipe_tower_filament"]\n'
        "[apps.a.profiles]\n"
        'vendor = "V"\nlibrary = "Lib"\nmachine = "Printer X"\nprocess = "0.20 Std @X"\n'
        'filament = "PETG @X"\n'
        "[overrides]\nenable-prime-tower = false\n"
    )
    return root


def test_flatten_merges_root_first_and_cleans(profile_tree: Path):
    src = ProfileSource(dir_fetcher(profile_tree), ["V", "Lib"])
    machine = flatten_profile(src, "machine", "Printer X", "V")
    assert machine.chain == ["V/machine/Printer X.json", "V/machine/m_base.json"]
    data = machine.data
    assert "inherits" not in data
    assert data["gcode_flavor"] == "klipper"  # leaf wins
    assert data["machine_pause_gcode"] == "M601"  # inherited from the root
    assert data["name"] == data["printer_settings_id"] == "Printer X"
    assert data["from"] == "system"
    assert data["instantiation"] == "true"


def test_flatten_filament_crosses_into_library(profile_tree: Path):
    src = ProfileSource(dir_fetcher(profile_tree), ["V", "Lib"])
    fil = flatten_profile(src, "filament", "PETG @X", "V")
    assert fil.chain == [
        "V/filament/PETG @X.json",
        "Lib/filament/PETG @System.json",
        "Lib/filament/base/fil_base.json",
    ]
    assert fil.data["filament_type"] == ["PETG"]
    assert fil.data["nozzle_temperature"] == ["250"]
    assert fil.data["filament_settings_id"] == ["PETG @X"]  # list form kept


def test_flatten_unindexed_parent_and_drop_keys(profile_tree: Path):
    src = ProfileSource(dir_fetcher(profile_tree), ["V", "Lib"])
    proc = flatten_profile(src, "process", "0.20 Std @X", "V", drop=("wipe_tower_filament",))
    assert proc.chain[-1] == "V/process/p_common.json"
    assert proc.data["layer_height"] == "0.2"
    assert proc.data["enable_prime_tower"] == "1"
    assert "wipe_tower_filament" not in proc.data
    assert proc.data["print_settings_id"] == "0.20 Std @X"


def test_missing_parent_and_cycle(profile_tree: Path):
    src = ProfileSource(dir_fetcher(profile_tree), ["V", "Lib"])
    with pytest.raises(ProfileError, match="not found"):
        flatten_profile(src, "process", "nope", "V")
    _write(profile_tree / "V" / "process" / "loop.json", {"name": "loop", "inherits": "loop"})
    with pytest.raises(ProfileError, match="cycle"):
        flatten_profile(src, "process", "loop", "V")


def test_sync_profiles_writes_files(profile_tree: Path, mini_printer: Path):
    machine = load_machine(mini_printer)
    written = sync_profiles(machine, fetch=dir_fetcher(profile_tree))
    names = sorted(p.name for p in written)
    assert names == ["filament.json", "machine.json", "process.json", "source.json"]
    out = mini_printer / "profiles" / "a"
    process = json.loads((out / "process.json").read_text())
    assert "wipe_tower_filament" not in process
    source = json.loads((out / "source.json").read_text())
    assert source["tag"] == "v1.0"
    assert source["chains"]["filament"][-1] == "Lib/filament/base/fil_base.json"


def test_sync_profiles_extra_filaments(profile_tree: Path, mini_printer: Path):
    toml = mini_printer / "machine.toml"
    toml.write_text(
        toml.read_text().replace(
            "[overrides]", '[apps.a.profiles.filaments]\nPLA = "fil_base"\n[overrides]'
        )
    )
    machine = load_machine(mini_printer)
    written = sync_profiles(machine, fetch=dir_fetcher(profile_tree))
    assert "filament-PLA.json" in {p.name for p in written}
    assert machine.filament_profile("PLA").name == "filament-PLA.json"
    assert machine.filament_profile("PETG").name == "filament.json"
    source = json.loads((mini_printer / "profiles" / "a" / "source.json").read_text())
    assert source["chains"]["filament-PLA"] == ["Lib/filament/base/fil_base.json"]


def test_load_machine_validates(mini_printer: Path):
    machine = load_machine(mini_printer)
    assert machine.slicer().appimage_name == "A_V1.0.AppImage"
    assert machine.overrides == {"enable-prime-tower": False}
    with pytest.raises(MachineConfigError):
        machine.slicer("missing")
    with pytest.raises(MachineConfigError):
        load_machine(mini_printer / "nope")


def test_u1_machine_toml():
    machine = load_machine(U1)
    assert machine.app == "orca"
    assert machine.pause_command == "M600"
    assert machine.overrides["enable-prime-tower"] is False
    assert machine.overrides["sparse-infill-density"] == "100%"
    assert machine.slicer().tag == "v2.5.1"
    assert machine.slicer("snapmaker").tag == "v2.4.1"


@pytest.mark.parametrize("app", ["orca", "snapmaker"])
def test_committed_u1_profiles(app: str):
    paths = load_machine(U1).profile_paths(app)
    data = {kind: json.loads(path.read_text()) for kind, path in paths.items()}
    for profile in data.values():
        assert "inherits" not in profile
    assert data["process"]["layer_height"] == "0.2"
    assert data["process"]["enable_prime_tower"] == "1"  # stock; turned off by the override
    assert data["machine"]["gcode_flavor"] == "klipper"
    assert data["machine"]["machine_pause_gcode"] == "M600"
    assert data["machine"]["thumbnails"] == "48x48/PNG, 300x300/PNG"
    assert data["filament"]["filament_type"] == ["PETG"]
    assert data["process"]["compatible_printers"] == [data["machine"]["name"]]
    assert ("wipe_tower_filament" in data["process"]) == (app == "orca")


def test_sync_profiles_cli_unknown_printer(tmp_path: Path):
    result = CliRunner().invoke(make_app(), ["sync-profiles", "nope", "--spec-root", str(tmp_path)])
    assert result.exit_code == 1
    assert "machine.toml not found" in result.output


@pytest.mark.integration
@pytest.mark.parametrize("app", ["orca", "snapmaker"])
def test_github_sync_matches_committed(app: str):
    machine = load_machine(U1)
    slicer = machine.slicer(app)
    flat = flatten_app(slicer, github_fetcher(slicer.repo, slicer.tag))
    paths = machine.profile_paths(app)
    for material in slicer.profiles.filaments:
        paths[f"filament-{material}"] = machine.filament_profile(material, app)
    for kind, path in paths.items():
        assert flat[kind].data == json.loads(path.read_text()), kind
