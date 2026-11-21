"""W4/W11 (#65, #121): the config editor's pure TS helpers (save payload, schema hints).

Runs `node --test` on web/tests/paths.test.ts and params-schema.test.ts. Node's own type
stripping runs the TS, so no npm test runner is needed; skipped without Node >= 22.6.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TESTS = [REPO / "web" / "tests" / "paths.test.ts", REPO / "web" / "tests" / "params-schema.test.ts"]


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


def test_editor_helpers():
    out = subprocess.run(
        [NODE, "--test", *map(str, TESTS)], capture_output=True, text=True, cwd=REPO / "web"
    )
    assert out.returncode == 0, out.stdout + out.stderr
