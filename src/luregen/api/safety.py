"""Path-parameter checks: every name is one safe segment and every path stays inside its root."""

import re
from pathlib import Path
from typing import Annotated

from fastapi import HTTPException
from fastapi import Path as PathParam

NAME_PATTERN = r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$"
_NAME = re.compile(NAME_PATTERN)

# One path segment: letters, digits, `.`, `_`, `-`; no leading dot (hides `.jobs`, `..`).
Name = Annotated[str, PathParam(pattern=NAME_PATTERN, max_length=128)]


def check_name(value: str, what: str = "name") -> str:
    if not _NAME.fullmatch(value) or ".." in value:
        raise HTTPException(400, f"invalid {what}: {value!r}")
    return value


def check_relpath(value: str, what: str = "path") -> list[str]:
    """Split a `/`-separated relative path and check every segment."""
    parts = value.split("/")
    for part in parts:
        check_name(part, what)
    return parts


def inside(root: Path, path: Path) -> Path:
    """`path` resolved (symlinks followed); 404 unless it stays inside `root`."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise HTTPException(404, "not found")
    return resolved
