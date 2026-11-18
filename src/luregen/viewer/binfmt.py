"""Compact binary toolpath format for the web viewers (#64), version 1.

All numbers are little-endian. A file is::

    offset  size  field
    0       4     magic  b"LGTP"
    4       2     u16    format version (1)
    6       2     u16    flags (0, reserved)
    8       4     u32    header_len: bytes of UTF-8 JSON that follow, space-padded so the
                         data section starts on an 8-byte boundary
    12      …     JSON header
    D = 12 + header_len: data section; typed arrays, each starting on an 8-byte boundary

The header's `arrays` list describes every array: `{"name", "dtype", "offset", "count",
"components"}`, with `offset` counted from the **start of the file** (a TS reader does
`new Float32Array(buf, offset, count * components)`). N is the segment count
(`header.segments`); arrays always appear in this order:

    name      dtype    count  meaning
    points    float32  N+1×3  polyline xyz in mm; segment i runs points[i] → points[i+1]
    kind      uint8    N      index into header.kinds (0 is always "jump": never drawn)
    tool      uint8    N      index into header.tools (`number` is the T number)
    feed      float32  N      mm/min (0 for jumps and CNC rapids)
    line      uint32   N      1-based source line of the move (for G-code listing sync)
    feature   uint8    N      print only: index into header.features (from ;TYPE:)
    layer     uint16   N      print only: index into header.layers (0 = before layer 1)
    width     uint16   N      print only: extrusion width in µm (0 when not extruding)
    height    uint16   N      print only: layer height in µm (from ;HEIGHT:)
    time      float32  N×2    CNC only: job time in s at the segment's start and end

Print headers add `layers` (`[{z, height, start, line}]`, entry 0 is the start G-code),
`layer_offsets` (len(layers)+1 prefix offsets: layer i is segments
[layer_offsets[i], layer_offsets[i+1])) and `layer_count`. Both kinds carry `kinds`, `tools`,
`markers` (tool changes and pauses with their `segment` index), `bounds`, `bounds_active`,
`stats`, `source` and `params`. See docs/viewer-binary-format.md.
"""

import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any, Literal

import numpy as np

from luregen.viewer.toolpath import (
    DEFAULT_MAX_ARC_SEGMENTS,
    DEFAULT_TOLERANCE,
    Toolpath,
    detect_kind,
    extract,
)

MAGIC = b"LGTP"
VERSION = 1
_PREFIX = struct.Struct("<4sHHI")
_ALIGN = 8

# (array name, Toolpath attribute, dtype, components); print-only arrays last.
_ARRAYS: tuple[tuple[str, str, str, int], ...] = (
    ("points", "points", "float32", 3),
    ("kind", "seg_kind", "uint8", 1),
    ("tool", "tool", "uint8", 1),
    ("feed", "feed", "float32", 1),
    ("line", "line", "uint32", 1),
    ("feature", "feature", "uint8", 1),
    ("layer", "layer", "uint16", 1),
    ("width", "width", "uint16", 1),
    ("height", "height", "uint16", 1),
    ("time", "time", "float32", 2),
)
# Bumped when the encoder's output changes within format v1 (new optional arrays or header
# keys), so cached `.bin` files written by an older encoder are rebuilt.
ENCODER_REVISION = 2


class ToolpathFormatError(ValueError):
    pass


def _pad(n: int) -> int:
    return -n % _ALIGN


def encode(tp: Toolpath) -> bytes:
    """Serialize a toolpath to the binary format."""
    blobs: list[tuple[str, str, int, int, bytes]] = []
    for name, attr, dtype, comps in _ARRAYS:
        arr = getattr(tp, attr)
        if arr is None:
            continue
        if comps > 1 and (arr.ndim != 2 or arr.shape[1] != comps):
            raise ToolpathFormatError(f"{name} must have shape (n, {comps})")
        data = np.ascontiguousarray(arr, dtype=np.dtype(dtype).newbyteorder("<")).tobytes()
        blobs.append((name, dtype, len(arr), comps, data))

    header: dict[str, Any] = {"format": "luregen-toolpath", "version": VERSION, "kind": tp.kind}
    header.update(tp.meta)
    header["segments"] = tp.n_segments

    # Offsets depend on the header length; iterate until the padded length is stable.
    table: list[dict[str, Any]] = []
    header_len = 0
    for _ in range(4):
        data_start = _PREFIX.size + header_len
        off = data_start
        table = []
        for name, dtype, count, comps, data in blobs:
            off += _pad(off)
            table.append(
                {"name": name, "dtype": dtype, "offset": off, "count": count, "components": comps}
            )
            off += len(data)
        header["arrays"] = table
        raw = json.dumps(header, separators=(",", ":"), allow_nan=False).encode()
        needed = len(raw) + _pad(_PREFIX.size + len(raw))
        if needed == header_len:
            break
        header_len = needed
    else:  # pragma: no cover - converges in two passes
        raise ToolpathFormatError("header length did not converge")

    out = bytearray(_PREFIX.pack(MAGIC, VERSION, 0, header_len))
    out += raw + b" " * (header_len - len(raw))
    for entry, (*_, data) in zip(table, blobs, strict=True):
        out += b"\0" * (entry["offset"] - len(out))
        out += data
    return bytes(out)


def read_header(data: bytes) -> dict[str, Any]:
    """Parse the prefix and JSON header (`data` may be just the start of a file)."""
    if len(data) < _PREFIX.size:
        raise ToolpathFormatError("truncated toolpath file")
    magic, version, _flags, header_len = _PREFIX.unpack_from(data)
    if magic != MAGIC:
        raise ToolpathFormatError("not a luregen toolpath file")
    if version != VERSION:
        raise ToolpathFormatError(f"unsupported toolpath version {version}")
    end = _PREFIX.size + header_len
    if len(data) < end:
        raise ToolpathFormatError("truncated toolpath header")
    return json.loads(data[_PREFIX.size : end])


def read_file_header(path: Path) -> dict[str, Any]:
    """Header of a `.bin` file without reading its arrays."""
    with open(path, "rb") as fh:
        prefix = fh.read(_PREFIX.size)
        if len(prefix) < _PREFIX.size:
            raise ToolpathFormatError("truncated toolpath file")
        header_len = _PREFIX.unpack(prefix)[3]
        return read_header(prefix + fh.read(header_len))


def decode(data: bytes) -> Toolpath:
    """Inverse of `encode` (arrays are read-only views of `data`)."""
    header = read_header(data)
    arrays: dict[str, np.ndarray] = {}
    for entry in header.pop("arrays"):
        dtype = np.dtype(entry["dtype"]).newbyteorder("<")
        n = entry["count"] * entry["components"]
        arr = np.frombuffer(data, dtype=dtype, count=n, offset=entry["offset"])
        if entry["components"] > 1:
            arr = arr.reshape(-1, entry["components"])
        arrays[entry["name"]] = arr
    for key in ("format", "version"):
        header.pop(key)
    kind = header.pop("kind")
    attrs = {attr: arrays.get(name) for name, attr, _, _ in _ARRAYS}
    return Toolpath(kind=kind, meta=header, **attrs)


# --- files --------------------------------------------------------------------------------


def cache_path(source: Path, kind: str) -> Path:
    """`<file>.<kind>.bin` next to the source."""
    source = Path(source)
    return source.with_name(f"{source.name}.{kind}.bin")


def resolve_kind(source: Path, kind: str = "auto") -> Literal["print", "cnc"]:
    if kind in ("print", "cnc"):
        return kind  # type: ignore[return-value]
    if kind != "auto":
        raise ValueError(f"unknown toolpath kind {kind!r}")
    with open(source, encoding="utf-8", errors="replace") as fh:
        return detect_kind(fh.read(65536))


def encode_file(
    source: Path,
    kind: str = "auto",
    out: Path | None = None,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    max_arc_segments: int = DEFAULT_MAX_ARC_SEGMENTS,
    force: bool = False,
) -> Path:
    """Encode `source` to `out` (default `<file>.<kind>.bin`), reusing a fresh cached file.

    The cache is reused when it is newer than the source and was written by this format
    version with the same parameters. Writes are atomic (temp file + rename).
    """
    source = Path(source)
    resolved = resolve_kind(source, kind)
    target = Path(out) if out is not None else cache_path(source, resolved)
    params = {"tolerance": tolerance, "max_arc_segments": max_arc_segments}
    if not force and _fresh(source, target, resolved, {**params, "encoder": ENCODER_REVISION}):
        return target
    text = source.read_text(encoding="utf-8", errors="replace")
    tp = extract(text, resolved, **params)
    tp.meta["source"] = {"name": source.name, **tp.meta.get("source", {})}
    tp.meta["params"]["encoder"] = ENCODER_REVISION
    data = encode(tp)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def _fresh(source: Path, target: Path, kind: str, params: dict[str, Any]) -> bool:
    try:
        if target.stat().st_mtime < source.stat().st_mtime:
            return False
        header = read_file_header(target)
    except (OSError, ToolpathFormatError, ValueError):
        return False
    cached = header.get("params", {})
    return header.get("kind") == kind and all(cached.get(k) == v for k, v in params.items())
