"""OrcaSlicer CLI runner (#32).

Finds the pinned slicer (on PATH, or as an AppImage extracted into the cache, downloading it
if needed), builds the `--slice` command from the flattened profiles and the machine's lure
overrides, and runs it. Each STL is sliced on its own, so every section gets its own G-code.

The upstream AppImages are Ubuntu 24.04 builds (glibc 2.38, host GTK/WebKit). On older hosts
the same extracted tree runs inside a small container (`tools/orca-runtime/Dockerfile`), with
the cache, profiles, inputs and output folder bind-mounted at their host paths.
"""

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from luregen.fab.printer.machine import PrinterMachine, SlicerApp

RUNTIME_DOCKERFILE = "tools/orca-runtime/Dockerfile"


class SlicerError(RuntimeError):
    pass


@dataclass
class SliceResult:
    stl: Path
    gcode: Path | None
    command: list[str]
    returncode: int
    log: Path
    result: dict[str, Any] = field(default_factory=dict)  # Orca's result.json, when written

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.gcode is not None and self.gcode.is_file()


# --- locating the binary ---------------------------------------------------------------


def app_cache_dir(machine: PrinterMachine, app: SlicerApp) -> Path:
    return machine.cache_dir / f"{app.key}-{app.version}"


def cached_apprun(machine: PrinterMachine, app: SlicerApp) -> Path:
    return app_cache_dir(machine, app) / "squashfs-root" / "AppRun"


def download(url: str, dest: Path, timeout: float = 60.0) -> Path:
    """Download `url` to `dest` via a `.part` file, so an interrupted fetch never looks done."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, part.open("wb") as out:
            shutil.copyfileobj(resp, out, length=1 << 20)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise SlicerError(f"download failed: {url}: {exc}") from exc
    part.replace(dest)
    return dest


def extract_appimage(appimage: Path) -> Path:
    """Run `<appimage> --appimage-extract` next to it; return the extracted AppRun."""
    appimage.chmod(appimage.stat().st_mode | 0o111)
    target = appimage.parent / "squashfs-root"
    if target.exists():
        shutil.rmtree(target)
    proc = subprocess.run(
        [str(appimage), "--appimage-extract"], cwd=appimage.parent, capture_output=True, text=True
    )
    apprun = target / "AppRun"
    if proc.returncode != 0 or not apprun.exists():
        raise SlicerError(f"extracting {appimage} failed: {proc.stderr.strip()[-500:]}")
    return apprun


def find_slicer(machine: PrinterMachine, app: SlicerApp, fetch: bool = True) -> Path:
    """Return the slicer executable: `$LUREGEN_SLICER`, PATH, the cache, or a fresh download."""
    if env := os.environ.get("LUREGEN_SLICER"):
        path = Path(env).expanduser()
        if not path.exists():
            raise SlicerError(f"LUREGEN_SLICER points to a missing file: {path}")
        return path
    if on_path := shutil.which(app.binary):
        return Path(on_path)
    apprun = cached_apprun(machine, app)
    if apprun.exists():
        return apprun
    appimage = app_cache_dir(machine, app) / app.appimage_name
    if not appimage.exists():
        if not fetch:
            raise SlicerError(
                f"{app.label} {app.version} not found: no `{app.binary}` on PATH and nothing in "
                f"{app_cache_dir(machine, app)}. Allow the download or fetch {app.appimage_url}"
            )
        download(app.appimage_url, appimage)
    return extract_appimage(appimage)


# --- runtime -------------------------------------------------------------------------------


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split(".") if p.isdigit())


def host_glibc() -> str | None:
    try:
        return os.confstr("CS_GNU_LIBC_VERSION").split()[-1]
    except (AttributeError, ValueError, OSError, IndexError):
        return None


def choose_runtime(machine: PrinterMachine, app: SlicerApp, slicer: Path | None = None) -> str:
    """`host` or `docker`, following `runtime.mode`.

    `auto` runs a natively installed slicer on the host; an extracted AppImage (`AppRun`) runs
    on the host only when its glibc is at least the app's `min_glibc`.
    """
    mode = os.environ.get("LUREGEN_SLICER_RUNTIME", machine.runtime.mode)
    if mode in ("host", "docker"):
        return mode
    if slicer is not None and slicer.name != "AppRun":
        return "host"
    glibc = host_glibc()
    if glibc is None or _version_tuple(glibc) >= _version_tuple(app.min_glibc):
        return "host"
    if shutil.which("docker"):
        return "docker"
    raise SlicerError(
        f"{app.label} {app.version} needs glibc >= {app.min_glibc} (host has {glibc}) and "
        f"docker is not available to run it in {RUNTIME_DOCKERFILE}"
    )


def docker_prefix(image: str, mounts: list[Path], workdir: Path) -> list[str]:
    """`docker run` arguments that run as the current user with `mounts` at their own paths."""
    # --init: xvfb-run must not be PID 1, or it never sees Xvfb's ready signal and hangs.
    cmd = ["docker", "run", "--rm", "--init", "--user", f"{os.getuid()}:{os.getgid()}"]
    cmd += ["-e", "HOME=/tmp", "-w", str(workdir)]
    for path in sorted({p.resolve() for p in mounts}):
        cmd += ["-v", f"{path}:{path}"]
    return [*cmd, image]


def ensure_docker_image(image: str, repo_root: Path) -> None:
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if probe.returncode == 0:
        return
    dockerfile = repo_root / RUNTIME_DOCKERFILE
    if not dockerfile.is_file():
        raise SlicerError(f"docker image {image} missing and {dockerfile} not found")
    proc = subprocess.run(
        ["docker", "build", "-t", image, str(dockerfile.parent)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SlicerError(f"docker build of {image} failed: {proc.stderr.strip()[-800:]}")


# --- command -------------------------------------------------------------------------------


def format_override(key: str, value: Any) -> str:
    """`--key=value`; booleans become 1/0 (Orca rejects `--key false`)."""
    if isinstance(value, bool):
        value = int(value)
    return f"--{key}={value}"


def build_command(
    slicer: Path,
    profiles: dict[str, Path],
    overrides: dict[str, Any],
    stl: Path,
    outdir: Path,
    xvfb: bool = True,
    filaments: list[Path] | None = None,
    filament_ids: str | None = None,
) -> list[str]:
    """The slicer argv for one STL. `xvfb` wraps it in `xvfb-run -a` (needs an X display).

    `filaments` replaces the single `filament` profile (one per tool, in slot order) and
    `filament_ids` assigns the object to one of them (1-based).
    """
    loaded = ";".join(str(p) for p in filaments) if filaments else str(profiles["filament"])
    ids = ["--load-filament-ids", filament_ids] if filament_ids else []
    cmd = [
        str(slicer),
        "--slice",
        "0",
        "--load-settings",
        f"{profiles['machine']};{profiles['process']}",
        "--load-filaments",
        loaded,
        *ids,
        *(format_override(k, v) for k, v in overrides.items()),
        "--outputdir",
        str(outdir),
        str(stl),
    ]
    return ["xvfb-run", "-a", *cmd] if xvfb else cmd


def slice_stl(
    machine: PrinterMachine,
    stl: Path,
    outdir: Path,
    app_key: str | None = None,
    extra_overrides: dict[str, Any] | None = None,
    fetch: bool = True,
    filaments: list[Path] | None = None,
    filament_ids: str | None = None,
    replace_overrides: bool = False,
) -> SliceResult:
    """Slice one STL to `<outdir>/<stl stem>.gcode`; the slicer's output goes to `.slice.log`.

    `extra_overrides` go on top of the machine's; with `replace_overrides` they are the whole
    set (a job plan that already merged them).
    """
    app = machine.slicer(app_key)
    stl = Path(stl).resolve()
    if stl.suffix.lower() != ".stl":
        raise SlicerError(f"feed STL, not {stl.suffix} ({stl.name})")
    if not stl.is_file():
        raise SlicerError(f"input not found: {stl}")
    profiles = {k: p.resolve() for k, p in machine.profile_paths(app.key).items()}
    filaments = [Path(p).resolve() for p in filaments] if filaments else None
    missing = [str(p) for p in [*profiles.values(), *(filaments or [])] if not p.is_file()]
    if missing:
        raise SlicerError(f"flattened profiles missing ({missing}); run `lure sync-profiles`")

    outdir = Path(outdir).resolve()
    work = outdir / f".slice-{stl.stem}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    slicer = find_slicer(machine, app, fetch=fetch).resolve()
    runtime = choose_runtime(machine, app, slicer)
    base = {} if replace_overrides else machine.overrides
    overrides = {**base, **(extra_overrides or {})}
    fil = {"filaments": filaments, "filament_ids": filament_ids}
    if runtime == "docker":
        ensure_docker_image(machine.runtime.docker_image, _repo_root(machine))
        mounts = [slicer.parent, profiles["machine"].parent, stl.parent, work]
        mounts += sorted({p.parent for p in filaments or []})
        argv = docker_prefix(machine.runtime.docker_image, mounts, work)
        # Time out inside the container too: killing the docker client leaves it running.
        argv += ["timeout", str(int(machine.runtime.timeout_s))]
        argv += build_command(slicer, profiles, overrides, stl, work, xvfb=True, **fil)
    else:
        argv = build_command(
            slicer, profiles, overrides, stl, work, xvfb=bool(shutil.which("xvfb-run")), **fil
        )

    log = outdir / f"{stl.stem}.slice.log"
    try:
        proc = subprocess.run(
            argv, cwd=work, capture_output=True, text=True, timeout=machine.runtime.timeout_s
        )
        returncode, output = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        returncode, output = -1, f"timed out after {exc.timeout}s\n"
    log.write_text("$ " + shlex.join(argv) + "\n\n" + output)

    result: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        result = json.loads((work / "result.json").read_text())
    gcode = None
    plates = sorted(work.glob("plate_*.gcode"))
    if returncode == 0 and plates:
        gcode = outdir / f"{stl.stem}.gcode"
        plates[0].replace(gcode)
    shutil.rmtree(work, ignore_errors=True)
    return SliceResult(stl, gcode, argv, returncode, log, result)


def slice_stls(
    machine: PrinterMachine, stls: list[Path], outdir: Path, **kwargs: Any
) -> list[SliceResult]:
    """Slice each STL separately (one G-code per section)."""
    return [slice_stl(machine, stl, outdir, **kwargs) for stl in stls]


def _repo_root(machine: PrinterMachine) -> Path:
    # spec/printers/<machine>/ → repo root
    return machine.root.resolve().parents[2]
