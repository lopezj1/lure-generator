"""Render preview thumbnails and embed them in G-code (#35).

The Orca CLI writes no thumbnails into plain `.gcode` (F6a finding), but the U1 screen and
Moonraker show the PNG blocks the GUI embeds (48×48 and 300×300, from the machine profile's
`thumbnails`). This module renders the oriented mesh itself, a small z-buffered rasterizer
in numpy with Lambert shading (no OpenGL, so it runs headless and in CI), and writes the
blocks after the G-code header:

    ; THUMBNAIL_BLOCK_START
    ;
    ; thumbnail begin 300x300 <base64 length>
    ; <base64, 78 characters per line>
    ; thumbnail end
    ;
    ; THUMBNAIL_BLOCK_END

Insertion replaces an existing block, so it is idempotent.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

import numpy as np

BLOCK_START = "; THUMBNAIL_BLOCK_START"
BLOCK_END = "; THUMBNAIL_BLOCK_END"
HEADER_END = "; HEADER_BLOCK_END"
LINE_LENGTH = 78
DEFAULT_SIZES = ((48, 48), (300, 300))
_SIZE_RE = re.compile(r"(\d+)x(\d+)(?:/(\w+))?")
# Camera: from the front-right, above (print coordinates, z up). Light comes from the camera.
VIEW_DIR = np.array([-0.55, 0.75, -0.45])
UP = np.array([0.0, 0.0, 1.0])
MAX_SUBDIV = 200  # samples per edge; long slivers (flat joint faces) need many


@dataclass(frozen=True)
class Thumb:
    width: int
    height: int
    png: bytes


def parse_sizes(spec: str | None) -> list[tuple[int, int]]:
    """Orca's `thumbnails` setting ("48x48/PNG, 300x300/PNG") → [(48, 48), (300, 300)]."""
    if not spec:
        return list(DEFAULT_SIZES)
    out = []
    for m in _SIZE_RE.finditer(spec):
        if (m.group(3) or "PNG").upper() == "PNG":
            out.append((int(m.group(1)), int(m.group(2))))
    return out or list(DEFAULT_SIZES)


def hex_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) != 6:
        return (128, 128, 128)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _camera(view_dir: np.ndarray) -> np.ndarray:
    """Rows: screen-right, screen-up, view direction (into the screen)."""
    d = view_dir / np.linalg.norm(view_dir)
    right = np.cross(d, UP)
    right /= np.linalg.norm(right)
    up = np.cross(right, d)
    return np.stack([right, up, d])


def render(
    vertices: np.ndarray,
    faces: np.ndarray,
    width: int,
    height: int,
    color: str = "#3C78D8",
    view_dir: np.ndarray = VIEW_DIR,
    margin: float = 0.06,
) -> np.ndarray:
    """RGBA image (height, width, 4) of the mesh; the background is transparent."""
    v = np.asarray(vertices, float)
    f = np.asarray(faces, int)
    img = np.zeros((height, width, 4), np.uint8)
    if len(f) == 0:
        return img
    cam = _camera(np.asarray(view_dir, float))
    p = v @ cam.T  # x right, y up, z depth
    lo, hi = p[:, :2].min(axis=0), p[:, :2].max(axis=0)
    span = max((hi - lo).max(), 1e-9)
    scale = (1 - 2 * margin) * min(width, height) / span
    mid = (lo + hi) / 2
    sx = (p[:, 0] - mid[0]) * scale + width / 2
    sy = height / 2 - (p[:, 1] - mid[1]) * scale
    sz = p[:, 2]

    tri = v[f]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    norm = np.linalg.norm(normals, axis=1)
    keep = norm > 1e-12
    normals[keep] /= norm[keep, None]
    facing = normals @ cam[2] < 0  # back faces (and the inner cavity shell) are hidden
    f, normals = f[facing & keep], normals[facing & keep]
    shade = 0.25 + 0.75 * np.clip(-(normals @ cam[2]), 0.0, 1.0)

    xs, ys, zs = sx[f], sy[f], sz[f]  # (n, 3)
    size = np.maximum(np.ptp(xs, axis=1), np.ptp(ys, axis=1))
    levels = np.clip(np.ceil(size * 2).astype(int), 1, MAX_SUBDIV)
    depth = np.full(width * height, np.inf)
    owner = np.full(width * height, -1)
    for k in np.unique(levels):
        idx = np.nonzero(levels == k)[0]
        i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
        ok = i + j <= k
        a, b = i[ok] / k, j[ok] / k
        bary = np.stack([1 - a - b, a, b], axis=1)  # (m, 3)
        px = xs[idx] @ bary.T
        py = ys[idx] @ bary.T
        pz = zs[idx] @ bary.T
        col = np.floor(px).astype(int)
        row = np.floor(py).astype(int)
        inside = (col >= 0) & (col < width) & (row >= 0) & (row < height)
        flat = (row * width + col)[inside]
        z = pz[inside]
        who = np.broadcast_to(idx[:, None], px.shape)[inside]
        order = np.lexsort((z, flat))  # nearest sample first per pixel
        flat, z, who = flat[order], z[order], who[order]
        first = np.ones(len(flat), bool)
        first[1:] = flat[1:] != flat[:-1]
        flat, z, who = flat[first], z[first], who[first]
        closer = z < depth[flat]
        depth[flat[closer]] = z[closer]
        owner[flat[closer]] = who[closer]

    hit = owner >= 0
    rgb = np.array(hex_rgb(color), float)
    lit = np.clip(shade[owner[hit], None] * rgb[None, :], 0, 255).astype(np.uint8)
    out = img.reshape(-1, 4)
    out[hit, :3] = lit
    out[hit, 3] = 255
    return out.reshape(height, width, 4)


def encode_png(rgba: np.ndarray) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def render_thumbnails(mesh, sizes: list[tuple[int, int]], color: str = "#3C78D8") -> list[Thumb]:
    return [
        Thumb(w, h, encode_png(render(mesh.vertices, mesh.faces, w, h, color))) for w, h in sizes
    ]


def thumbnail_block(thumbs: list[Thumb]) -> list[str]:
    lines = [BLOCK_START]
    for t in thumbs:
        data = base64.b64encode(t.png).decode("ascii")
        lines += [";", f"; thumbnail begin {t.width}x{t.height} {len(data)}"]
        lines += [f"; {data[i : i + LINE_LENGTH]}" for i in range(0, len(data), LINE_LENGTH)]
        lines.append("; thumbnail end")
    lines += [";", BLOCK_END, ""]
    return lines


def strip_thumbnails(lines: list[str]) -> list[str]:
    """Drop thumbnail blocks and the blank line `thumbnail_block` writes after each."""
    out, skip, after = [], False, False
    for line in lines:
        if line.startswith(BLOCK_START):
            skip = True
            continue
        if skip:
            if line.startswith(BLOCK_END):
                skip, after = False, True
            continue
        if after and line == "":
            after = False
            continue
        after = False
        out.append(line)
    return out


def inject_thumbnails(text: str, thumbs: list[Thumb]) -> str:
    """Put the thumbnail block after the header block (else at the top), replacing any."""
    lines = strip_thumbnails(text.splitlines())
    at = next((i + 1 for i, line in enumerate(lines) if line.startswith(HEADER_END)), 0)
    if at < len(lines) and lines[at] == "":
        at += 1  # keep the blank line after the header
    lines[at:at] = thumbnail_block(thumbs)
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + trailing


def decode_thumbnails(text: str) -> dict[str, bytes]:
    """{"WxH": png bytes} from a G-code's thumbnail blocks (round-trip check)."""
    out: dict[str, bytes] = {}
    key, data = None, []
    for line in text.splitlines():
        if m := re.match(r"^; thumbnail(?:_\w+)? begin (\d+)x(\d+) \d+", line):
            key, data = f"{m.group(1)}x{m.group(2)}", []
        elif key and line.startswith("; thumbnail") and line.rstrip().endswith("end"):
            out[key] = base64.b64decode("".join(data))
            key = None
        elif key and line.startswith("; "):
            data.append(line[2:].strip())
    return out
