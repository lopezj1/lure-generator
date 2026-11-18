"""TOML loading with `extends` layering and dotted `--set` overrides.

Precedence (low → high): `_common.toml` ← `family.toml` ← variant ← `--set`.
Each file may name its parent with `extends = "<path relative to this file>"`.
Tables merge recursively; scalars and lists are replaced whole.
"""

import copy
import os
import tomllib
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


def deep_merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_layered(path: Path) -> dict[str, Any]:
    """Load `path` and every ancestor named by `extends`, merged child-over-parent."""
    chain: list[Path] = []
    current: Path | None = path.resolve()
    while current is not None:
        if current in chain:
            raise ConfigError(f"extends cycle: {' -> '.join(map(str, [*chain, current]))}")
        if not current.is_file():
            raise ConfigError(f"config file not found: {current}")
        chain.append(current)
        data = tomllib.loads(current.read_text())
        parent = data.get("extends")
        current = (current.parent / parent).resolve() if parent else None

    merged: dict[str, Any] = {}
    for file in reversed(chain):
        data = tomllib.loads(file.read_text())
        data.pop("extends", None)
        merged = deep_merge(merged, absolutize_paths(data, file.parent))
    return merged


# Keys holding file paths; made absolute relative to the file that set them.
PATH_KEYS: frozenset[tuple[str, str]] = frozenset(
    {("profile", "image"), ("profile", "top_image"), ("profile", "preset")}
)


def absolutize_paths(data: dict[str, Any], base: Path) -> dict[str, Any]:
    """Make the `PATH_KEYS` values of `data` absolute (relative ones are taken from `base`).

    Mutates and returns `data`.
    """
    for section, key in PATH_KEYS:
        table = data.get(section)
        if isinstance(table, dict) and isinstance(table.get(key), str):
            table[key] = str((base / table[key]).resolve())
    return data


def relativize_paths(data: dict[str, Any], base: Path) -> dict[str, Any]:
    """Write the `PATH_KEYS` values of `data` relative to `base` (POSIX separators).

    The inverse of `absolutize_paths`; paths outside `base` get `..` segments. Mutates and
    returns `data`.
    """
    for section, key in PATH_KEYS:
        table = data.get(section)
        if isinstance(table, dict) and isinstance(table.get(key), str):
            table[key] = relative_path(table[key], base)
    return data


def relative_path(value: str | Path, base: Path) -> str:
    """`value` relative to `base` (POSIX), with `..` segments when it lies outside."""
    path = Path(value)
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return Path(os.path.relpath(path, base)).as_posix()


def parse_value(text: str) -> Any:
    """Parse an override value as TOML (numbers, bools, arrays, …), else keep it a string."""
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


def apply_overrides(data: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    """Apply `a.b.c=value` overrides. Numeric segments index into lists."""
    out = copy.deepcopy(data)
    for item in overrides:
        if "=" not in item:
            raise ConfigError(f"override must look like key.path=value: {item!r}")
        dotted, raw = item.split("=", 1)
        parts = dotted.strip().split(".")
        node: Any = out
        for part in parts[:-1]:
            node = _step(node, part, dotted, create=True)
        last = parts[-1]
        if isinstance(node, list):
            node[_index(node, last, dotted)] = parse_value(raw.strip())
        else:
            node[last] = parse_value(raw.strip())
    return out


def _step(node: Any, part: str, dotted: str, create: bool) -> Any:
    if isinstance(node, list):
        return node[_index(node, part, dotted)]
    if isinstance(node, dict):
        if part not in node:
            if not create:
                raise ConfigError(f"unknown key in override {dotted!r}: {part}")
            node[part] = {}
        return node[part]
    raise ConfigError(f"cannot descend into {part!r} in override {dotted!r}")


def _index(node: list[Any], part: str, dotted: str) -> int:
    if not part.isdigit() or int(part) >= len(node):
        raise ConfigError(f"bad list index {part!r} in override {dotted!r}")
    return int(part)
