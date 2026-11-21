"""Printer machine config: `spec/printers/<machine>/machine.toml`."""

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROFILE_KINDS = ("machine", "process", "filament")


class MachineConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProfileSet:
    """Stock profile names for one slicer app, resolved from `vendor` then `library`."""

    vendor: str
    library: str
    machine: str
    process: str
    filament: str
    # More filament profiles by material (`PLA = "Generic PLA @…"`), flattened next to
    # `filament.json` as `filament-<material>.json`; used by the filament slots (#51).
    filaments: dict[str, str] = field(default_factory=dict)

    def name(self, kind: str) -> str:
        if kind not in PROFILE_KINDS:
            raise ValueError(f"unknown profile kind {kind!r}")
        return getattr(self, kind)


@dataclass(frozen=True)
class SlicerApp:
    """One pinned slicer build: where its profiles and AppImage come from."""

    key: str
    label: str
    version: str
    repo: str
    tag: str
    binary: str
    appimage_url: str
    profiles: ProfileSet
    # Oldest host glibc the AppImage runs on; older hosts use the container runtime.
    min_glibc: str = "0"
    # Keys removed while flattening, to work around CLI bugs of this app.
    drop_keys: tuple[str, ...] = ()

    @property
    def appimage_name(self) -> str:
        return self.appimage_url.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Runtime:
    """Where the slicer runs: `host`, `docker`, or `auto` (host if its glibc is new enough)."""

    mode: str = "auto"
    docker_image: str = "luregen/orca-runtime:24.04"
    timeout_s: float = 600.0


@dataclass(frozen=True)
class FilamentSlot:
    """A loaded tool (`T<slot>`): its material (a key in spec/materials.toml) and color."""

    slot: int
    material: str
    color: str = "#808080"

    @property
    def tool(self) -> str:
        return f"T{self.slot}"


@dataclass(frozen=True)
class PrinterMachine:
    """A printer folder under `spec/printers/`, with its slicer apps and overrides."""

    root: Path
    name: str
    app: str
    cache_dir: Path
    pause_command: str
    apps: dict[str, SlicerApp]
    overrides: dict[str, Any] = field(default_factory=dict)
    runtime: Runtime = field(default_factory=Runtime)
    # What is loaded in each tool, by slot; the slicer gets one filament profile per slot.
    filaments: tuple[FilamentSlot, ...] = ()
    # Dissimilar support-interface material per body material (PLA↔PETG don't bond).
    interface_pairs: dict[str, str] = field(default_factory=dict)
    # Overrides added when a job has supports, and on top when the interface is dissimilar.
    support_overrides: dict[str, Any] = field(default_factory=dict)
    interface_overrides: dict[str, Any] = field(default_factory=dict)
    # Overrides added when a part prints in a given material (e.g. TPU for the fin, #123).
    material_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def slot(self, n: int) -> FilamentSlot:
        for s in self.filaments:
            if s.slot == n:
                return s
        raise MachineConfigError(f"{self.name}: no filament slot T{n} in machine.toml")

    def filament_profile(self, material: str, key: str | None = None) -> Path:
        """The flattened filament profile for `material`: `filament-<material>.json`, or
        `filament.json` when that one's `filament_type` is `material`."""
        folder = self.profile_dir(key)
        extra = folder / f"filament-{material}.json"
        if extra.is_file():
            return extra
        default = folder / "filament.json"
        if default.is_file():
            ftype = json.loads(default.read_text()).get("filament_type")
            types = ftype if isinstance(ftype, list) else [ftype]
            if material in types:
                return default
        raise MachineConfigError(
            f"{self.name}: no {material} filament profile in {folder}; add it under "
            f"[apps.{self.slicer(key).key}.profiles.filaments] and run `lure sync-profiles`"
        )

    def slicer(self, key: str | None = None) -> SlicerApp:
        key = key or self.app
        if key not in self.apps:
            raise MachineConfigError(f"{self.name}: unknown app {key!r} (have {sorted(self.apps)})")
        return self.apps[key]

    def profile_dir(self, key: str | None = None) -> Path:
        return self.root / "profiles" / self.slicer(key).key

    def profile_paths(self, key: str | None = None) -> dict[str, Path]:
        folder = self.profile_dir(key)
        return {kind: folder / f"{kind}.json" for kind in PROFILE_KINDS}


def load_machine(path: Path) -> PrinterMachine:
    """Load a printer folder (or its machine.toml) into a `PrinterMachine`."""
    path = Path(path)
    toml_path = path / "machine.toml" if path.is_dir() else path
    if not toml_path.is_file():
        raise MachineConfigError(f"machine.toml not found: {toml_path}")
    data = tomllib.loads(toml_path.read_text())
    try:
        apps = {
            key: SlicerApp(
                key=key,
                label=entry["label"],
                version=str(entry["version"]),
                repo=entry["repo"],
                tag=entry["tag"],
                binary=entry["binary"],
                appimage_url=entry["appimage_url"],
                profiles=ProfileSet(**entry["profiles"]),
                drop_keys=tuple(entry.get("drop_keys", ())),
                min_glibc=str(entry.get("min_glibc", "0")),
            )
            for key, entry in data["apps"].items()
        }
        machine = PrinterMachine(
            root=toml_path.parent,
            name=data["name"],
            app=data["app"],
            cache_dir=Path(data["cache_dir"]).expanduser(),
            pause_command=data["pause_command"],
            apps=apps,
            overrides=dict(data.get("overrides", {})),
            runtime=Runtime(**data.get("runtime", {})),
            filaments=tuple(
                FilamentSlot(
                    slot=int(f["slot"]),
                    material=str(f["material"]),
                    color=str(f.get("color", "#808080")),
                )
                for f in data.get("filaments", [])
            ),
            interface_pairs=dict(data.get("support_interface", {}).get("pairs", {})),
            support_overrides=dict(data.get("support_overrides", {})),
            interface_overrides=dict(data.get("interface_overrides", {})),
            material_overrides={k: dict(v) for k, v in data.get("material_overrides", {}).items()},
        )
    except (KeyError, TypeError) as exc:
        raise MachineConfigError(f"{toml_path}: missing or bad key: {exc}") from exc
    machine.slicer()  # validates `app`
    slots = [f.slot for f in machine.filaments]
    if len(set(slots)) != len(slots):
        raise MachineConfigError(f"{toml_path}: duplicate filament slots {slots}")
    if machine.runtime.mode not in ("auto", "host", "docker"):
        raise MachineConfigError(f"{toml_path}: runtime.mode must be auto, host or docker")
    return machine
