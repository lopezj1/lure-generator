"""Debug overlay: contour, rails, station lines and scale annotation drawn over the photo."""

from pathlib import Path

import cv2
import numpy as np

from luregen.profile.pipeline import ProfileResult, View

RAW = (160, 160, 160)
DORSAL = (40, 40, 230)
VENTRAL = (230, 120, 30)
STATION = (40, 180, 40)
MARKER = (200, 0, 200)
TEXT = (20, 20, 20)


def _poly(img: np.ndarray, pts: np.ndarray, color, thickness: int, closed: bool = False) -> None:
    cv2.polylines(
        img, [np.round(pts).astype(np.int32)], closed, color, thickness, lineType=cv2.LINE_AA
    )


def _label(img: np.ndarray, text: str, org: tuple[int, int], scale: float) -> None:
    thick = max(1, round(2 * scale))
    (w, h), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    x, y = org
    cv2.rectangle(img, (x - 4, y - h - 4), (x + w + 4, y + base + 2), (255, 255, 255), -1)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, TEXT, thick, cv2.LINE_AA)


def draw_view(
    view: View, stations_x: np.ndarray | None = None, title: str = "", span: str = "depth"
) -> np.ndarray:
    img = (
        view.image.copy() if view.image.ndim == 3 else cv2.cvtColor(view.image, cv2.COLOR_GRAY2BGR)
    )
    lw = max(1, round(max(img.shape[:2]) / 600))
    font = max(0.5, max(img.shape[:2]) / 1600)

    _poly(img, view.raw_px, RAW, lw, closed=True)
    r = view.rails
    xs = np.concatenate([[r.nose[0]], r.x, [r.tail[0]]])
    top = np.concatenate([[r.nose[1]], r.top, [r.tail[1]]])
    bot = np.concatenate([[r.nose[1]], r.bottom, [r.tail[1]]])
    to_px = view.frame.to_px
    _poly(img, to_px(np.column_stack([xs, top])), DORSAL, lw + 1)
    _poly(img, to_px(np.column_stack([xs, bot])), VENTRAL, lw + 1)

    if stations_x is not None:
        st, sb = r.at(stations_x)
        for x, zt, zb in zip(stations_x, st, sb, strict=True):
            _poly(img, to_px(np.array([[x, zt], [x, zb]])), STATION, lw)
    for tip in (r.nose, r.tail):
        c = np.round(to_px(np.array(tip))).astype(int)
        cv2.circle(img, (int(c[0]), int(c[1])), 3 * lw, STATION, -1, cv2.LINE_AA)
    if view.scale.marker_px is not None:
        _poly(img, view.scale.marker_px, MARKER, lw + 1, closed=True)

    lines = [
        title,
        view.scale.describe(),
        f"length {r.length:.1f} mm, max {span} {r.max_depth:.1f} mm",
    ]
    step = int(36 * font)
    for i, text in enumerate(t for t in lines if t):
        _label(img, text, (10, step * (i + 1)), font)
    return img


def render_overlay(result: ProfileResult) -> np.ndarray:
    """Side view with stations; the top view (if any) is stacked underneath."""
    t = result.table
    side = draw_view(result.side, t.x, f"side: {len(t)} stations, width from {t.meta['width']}")
    if result.top is None:
        return side
    xt = result.top.rails.nose[0] + t.x / t.length * result.top.rails.length
    top = draw_view(result.top, xt, "top view (width)", span="width")
    w = max(side.shape[1], top.shape[1])
    pad = [
        cv2.copyMakeBorder(v, 0, 0, 0, w - v.shape[1], cv2.BORDER_CONSTANT, value=(255,) * 3)
        for v in (side, top)
    ]
    return np.vstack(pad)


def write_debug(result: ProfileResult, out_dir: Path) -> list[Path]:
    """Write profile_debug.png and profile_mask.png (plus the top-view mask) into `out_dir`."""
    paths = [out_dir / "profile_debug.png", out_dir / "profile_mask.png"]
    cv2.imwrite(str(paths[0]), render_overlay(result))
    cv2.imwrite(str(paths[1]), result.side.mask)
    if result.top is not None:
        paths.append(out_dir / "profile_mask_top.png")
        cv2.imwrite(str(paths[2]), result.top.mask)
    return paths
