"""Machine-readable progress for job runners (`lure --progress json …`).

With `LUREGEN_PROGRESS=1`, commands print one-line JSON events to stderr:

    @@progress {"stage": "envelope", "frac": 0.5, "msg": "lofting"}
    @@result {"build_dir": "builds/glide/v/20260101-120000-abc1234"}

Without it, both functions are no-ops, so commands can call them unconditionally.
"""

import json
import os
import sys
from typing import Any

ENV = "LUREGEN_PROGRESS"
PROGRESS_PREFIX = "@@progress "
RESULT_PREFIX = "@@result "


def enabled() -> bool:
    return os.environ.get(ENV) == "1"


def _emit(prefix: str, data: dict[str, Any]) -> None:
    if enabled():
        print(prefix + json.dumps(data, default=str), file=sys.stderr, flush=True)


def emit(stage: str, frac: float | None = None, msg: str = "") -> None:
    """Report progress of `stage`; `frac` is 0–1 within the stage, or None if unknown."""
    _emit(PROGRESS_PREFIX, {"stage": stage, "frac": frac, "msg": msg})


def result(**data: Any) -> None:
    """Report the command's final machine-readable result (paths are stringified)."""
    _emit(RESULT_PREFIX, data)
