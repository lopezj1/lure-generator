"""W6/W7 (#67, #68): the TS toolpath reader and CNC job model agree with the encoder.

Runs `node --test web/tests/toolpath-reader.test.ts` (and `cnc-model.test.ts`) on
Python-encoded fixtures. Node's own
type stripping runs the TS, so no npm test runner is needed; skipped without Node >= 22.6.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from luregen.viewer.binfmt import decode, encode_file

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
TEST = REPO / "web" / "tests" / "toolpath-reader.test.ts"
CNC_TEST = REPO / "web" / "tests" / "cnc-model.test.ts"
GOLDEN = REPO / "tests" / "golden"


def _node() -> str | None:
    node = shutil.which("node") or str(Path.home() / ".local" / "bin" / "node")
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    major, minor = (int(x) for x in re.findall(r"\d+", out.stdout)[:2])
    return node if (major, minor) >= (22, 6) else None


NODE = _node()
pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node >= 22.6 (type stripping)")


def _case(bin_path: Path) -> dict:
    tp = decode(bin_path.read_bytes())
    arrays = {}
    for name, attr in (
        ("points", "points"),
        ("kind", "seg_kind"),
        ("tool", "tool"),
        ("feed", "feed"),
        ("line", "line"),
        ("feature", "feature"),
        ("layer", "layer"),
        ("width", "width"),
        ("height", "height"),
        ("time", "time"),
    ):
        arr = getattr(tp, attr)
        if arr is None:
            continue
        flat = arr.reshape(-1)
        arrays[name] = {
            "length": int(flat.size),
            "sum": float(flat.astype(np.float64).sum()),
            "first": float(flat[0]) if flat.size else 0.0,
            "last": float(flat[-1]) if flat.size else 0.0,
        }
    pts = tp.points
    markers = tp.meta["markers"]
    pause_layers = {m.get("layer") for m in markers if m["type"] == "pause"}
    return {
        "bin": str(bin_path),
        "kind": tp.kind,
        "segments": tp.n_segments,
        "arrays": arrays,
        "first_point": pts[0].tolist() if len(pts) else [],
        "last_point": pts[-1].tolist() if len(pts) else [],
        "layer_offsets": tp.meta.get("layer_offsets"),
        "markers": len(markers),
        "pauses": len(pause_layers),
    }


def test_ts_reader_matches_encoder(tmp_path: Path):
    sources = [
        FIXTURES / "orca_u1_multimaterial_excerpt.gcode",
        FIXTURES / "orca_2.5.1_cube.gcode",
        FIXTURES / "fusion_3drough_excerpt.nc",
        GOLDEN / "two_tool.nc",
    ]
    copies = [shutil.copy(src, tmp_path / src.name) for src in sources]
    # A pause in the middle of layer 2 (the excerpt has none).
    lines = (FIXTURES / "orca_u1_multimaterial_excerpt.gcode").read_text().splitlines()
    second = [i for i, line in enumerate(lines) if line.startswith(";LAYER_CHANGE")][1]
    lines.insert(second + 40, "M600")
    paused = tmp_path / "paused.gcode"
    paused.write_text("\n".join(lines) + "\n")
    cases = [_case(encode_file(Path(p))) for p in [*copies, paused]]
    assert cases[-1]["pauses"] == 1
    (tmp_path / "cases.json").write_text(json.dumps(cases))

    env = {**os.environ, "LUREGEN_READER_CASES": str(tmp_path / "cases.json")}
    res = subprocess.run(
        [NODE, "--experimental-strip-types", "--no-warnings", "--test", str(TEST)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert re.search(r"^\S+ pass 2$", res.stdout, re.M), res.stdout  # perf test skipped


def _probes(tp, part: int, offset: float) -> list[dict]:
    """Job time → expected tool tip: mid-move, and inside each tool-change/dwell gap."""
    numbers = [t["number"] for t in tp.meta["tools"]]
    time, pts = tp.time.astype(np.float64), tp.points.astype(np.float64)
    drawn = np.flatnonzero(tp.seg_kind != 0)
    out = []
    for s in drawn[:: max(1, len(drawn) // 15)]:
        t0, t1 = time[s]
        if t1 - t0 < 1e-3:
            continue
        out.append(
            {
                "t": offset + (t0 + t1) / 2,
                "pos": ((pts[s] + pts[s + 1]) / 2).tolist(),
                "part": part,
                "waiting": False,
                "tool_number": numbers[tp.tool[s]],
            }
        )
    for s in range(1, tp.n_segments):
        gap = time[s, 0] - time[s - 1, 1]
        if gap > 1 and tp.seg_kind[s] != 0:
            out.append(
                {
                    "t": offset + time[s - 1, 1] + gap / 2,
                    "pos": pts[s].tolist(),
                    "part": part,
                    "waiting": True,
                    "tool_number": numbers[tp.tool[s]],
                }
            )
    return out


def test_ts_cnc_model(tmp_path: Path):
    """W7 (#68): job building, playhead and stepping over one file and a two-file job."""
    jobs = [["two_tool.nc"], ["profile_arcs.nc", "two_tool.nc", "peck_drill.nc"]]
    cases = []
    for names in jobs:
        files, probes, numbers, offset, changes, markers = [], [], [], 0.0, 0, 0
        for part, name in enumerate(names):
            src = Path(shutil.copy(GOLDEN / name, tmp_path / name))
            tp = decode(encode_file(src).read_bytes())
            files.append({"label": name, "bin": str(encode_file(src))})
            probes += _probes(tp, part, offset)
            for t in tp.meta["tools"]:
                if t["number"] not in numbers:
                    numbers.append(t["number"])
            stats = tp.meta["stats"]
            offset += max(stats["time_s"], float(tp.time[-1, 1]))
            changes += stats["tool_changes"]
            markers += len(tp.meta["markers"]) + (1 if len(names) > 1 else 0)
        assert any(p["waiting"] for p in probes)
        cases.append(
            {
                "files": files,
                "duration": offset,
                "tool_numbers": numbers,
                "tool_changes": changes,
                "markers": markers,
                "probes": probes,
            }
        )
    (tmp_path / "cnc.json").write_text(json.dumps(cases))
    env = {**os.environ, "LUREGEN_CNC_CASES": str(tmp_path / "cnc.json")}
    res = subprocess.run(
        [NODE, "--experimental-strip-types", "--no-warnings", "--test", str(CNC_TEST)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert re.search(r"^\S+ pass 2$", res.stdout, re.M), res.stdout
