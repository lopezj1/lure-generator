"""Build output folders: builds/<family>/<variant>/<YYYYMMDD-HHMMSS>-<gitsha>[-dirty]/."""

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from luregen.config import LureConfig, dump_resolved
from luregen.units import g_to_oz

REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GitInfo:
    sha: str
    dirty: bool


def git_info(cwd: Path) -> GitInfo:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        sha = run("rev-parse", "--short=7", "HEAD")
        dirty = bool(run("status", "--porcelain", "--untracked-files=no"))
    except (subprocess.CalledProcessError, FileNotFoundError):
        return GitInfo(sha="nogit", dirty=False)
    return GitInfo(sha=sha, dirty=dirty)


def create_build_dir(cfg: LureConfig, out_root: Path, now: datetime | None = None) -> Path:
    """Create a new build folder, point `latest` at it, and write resolved.toml + report stubs."""
    now = now or datetime.now()
    git = git_info(out_root.parent if out_root.parent.exists() else Path.cwd())
    stamp = f"{now:%Y%m%d-%H%M%S}-{git.sha}" + ("-dirty" if git.dirty else "")
    variant_dir = out_root / cfg.family / cfg.name
    build = variant_dir / stamp
    build.mkdir(parents=True, exist_ok=False)

    (build / "resolved.toml").write_text(dump_resolved(cfg))
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "variant": cfg.name,
        "family": cfg.family,
        "created": now.isoformat(timespec="seconds"),
        "git": {"sha": git.sha, "dirty": git.dirty},
        "status": "config_only",
        "warnings": _config_warnings(cfg),
        "sections": {},
        "checks": [],
    }
    (build / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (build / "report.md").write_text(render_report_md(cfg, report))

    latest = variant_dir / "latest"
    tmp = variant_dir / ".latest.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(stamp, target_is_directory=True)
    os.replace(tmp, latest)
    return build


def update_report(build: Path, cfg: LureConfig, **fields) -> dict:
    """Merge top-level `fields` into report.json and re-render report.md."""
    path = build / "report.json"
    report = json.loads(path.read_text())
    report.update(fields)
    path.write_text(json.dumps(report, indent=2) + "\n")
    (build / "report.md").write_text(render_report_md(cfg, report))
    return report


def _config_warnings(cfg: LureConfig) -> list[str]:
    return [
        f"hardware {h.id!r} uses catalog placeholder masses (measured = false)"
        for h in cfg.hardware
        if not h.measured
    ]


def render_report_md(cfg: LureConfig, report: dict) -> str:
    lines = [
        f"# {cfg.name} ({cfg.family})",
        "",
        f"Built {report['created']} from `{report['git']['sha']}`"
        + (" (dirty tree)" if report["git"]["dirty"] else ""),
        f"Status: **{report['status']}**",
        "",
        "## Hardware",
        "",
        "| Item | Section | Qty | Mass (g) | Mass (oz) | Measured |",
        "|---|---|---|---|---|---|",
    ]
    for h in cfg.hardware:
        lo, hi = h.mass_g or (0.0, 0.0)
        lines.append(
            f"| {h.name} | {h.section} | {h.qty} | {lo:.2f}–{hi:.2f} | "
            f"{g_to_oz(lo):.3f}–{g_to_oz(hi):.3f} | {'yes' if h.measured else 'no'} |"
        )
    if env := report.get("envelope"):
        lines += ["", "## Envelope", "", *_envelope_md(env)]
    if report["warnings"]:
        lines += ["", "## Warnings", "", *(f"- {w}" for w in report["warnings"])]
    return "\n".join(lines) + "\n"


def _envelope_md(env: dict) -> list[str]:
    bb, c = env["bbox_mm"]["size"], env["centroid_mm"]
    lines = [
        f"- Style: {env['style']}"
        + (f" ({env['section_shape']} sections)" if env.get("section_shape") else ""),
        f"- Volume: {env['volume_cm3']:.2f} cm³; solid mass at {env['material_density']} g/cm³: "
        f"{env['solid_mass_g']:.1f} g ({g_to_oz(env['solid_mass_g']):.2f} oz)",
        f"- Bounding box (x × y × z): {bb[0]:.1f} × {bb[1]:.1f} × {bb[2]:.1f} mm",
        f"- Centroid: ({c[0]:.2f}, {c[1]:.2f}, {c[2]:.2f}) mm",
    ]
    if ro := env.get("roundover"):
        lines.append(f"- {ro['note']}")
    return lines


def resolve_build_dir(path: Path) -> Path:
    """Accept a build folder, a `latest` link, or a variant folder (uses its `latest`)."""
    path = path.resolve()
    if (path / "resolved.toml").is_file():
        return path
    if (path / "latest" / "resolved.toml").is_file():
        return (path / "latest").resolve()
    raise FileNotFoundError(f"{path} is not a build folder (no resolved.toml)")
