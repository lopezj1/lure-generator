"""Silhouette extraction: image → mask → contour → smoothed outline → dorsal/ventral rails.

Pixel coordinates are OpenCV's (x right, y down). `body_frame` maps a contour into the
package frame (x from the nose tip toward the tail, z up, nose tip at the origin) so rails
and stations never deal with image orientation.
"""

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np


class ProfileError(ValueError):
    pass


NoseSide = Literal["left", "right"]
SmoothMethod = Literal["efd", "spline"]


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def segment(
    image: np.ndarray,
    *,
    use_rembg: bool = False,
    exclude: list[np.ndarray] | None = None,
) -> np.ndarray:
    """Binary mask (uint8, 255 = subject) of the largest foreground blob, holes filled.

    Otsu threshold on a lightly blurred grayscale image. The background is whichever class
    dominates the image border, so dark-on-light and light-on-dark photos both work.
    `exclude` polygons (pixel coordinates, e.g. a scale marker) are removed from the mask.
    """
    if use_rembg:
        try:
            import rembg  # noqa: F401
        except ImportError as exc:
            raise ProfileError(
                "rembg segmentation was requested but rembg is not installed; "
                "use the default Otsu segmentation on a plain background"
            ) from exc
        raise ProfileError("rembg segmentation is not available in this build")

    gray = cv2.GaussianBlur(to_gray(image), (5, 5), 0)
    _, fg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    border = np.concatenate([fg[0], fg[-1], fg[:, 0], fg[:, -1]])
    if np.count_nonzero(border) > border.size / 2:
        fg = cv2.bitwise_not(fg)

    for poly in exclude or []:
        cv2.fillPoly(fg, [np.round(poly).astype(np.int32)], 0)

    k = max(3, (min(fg.shape) // 200) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        raise ProfileError("no foreground silhouette found")
    largest = max(contours, key=cv2.contourArea)
    mask = np.zeros_like(fg)
    cv2.drawContours(mask, [largest], -1, 255, thickness=cv2.FILLED)
    return mask


def largest_contour(mask: np.ndarray) -> np.ndarray:
    """Sub-pixel outer boundary of the mask as an (N, 2) x, y array (not repeated at the end).

    The 0.5 iso-line of the binary mask runs along pixel edges rather than through boundary
    pixel centers, which removes the half-pixel inward bias of `cv2.findContours`.
    """
    from skimage.measure import find_contours

    padded = np.pad((mask > 0).astype(float), 1)
    contours = find_contours(padded, 0.5)
    if not contours:
        raise ProfileError("mask is empty")
    rc = max(contours, key=len)
    xy = rc[:, ::-1] - 1.0  # (row, col) → (x, y), undo the padding
    if np.allclose(xy[0], xy[-1]):
        xy = xy[:-1]
    if len(xy) < 16:
        raise ProfileError("silhouette is too small to trace")
    return xy


def resample_closed(points: np.ndarray, n: int) -> np.ndarray:
    """Resample a closed polyline to `n` points evenly spaced in arc length."""
    closed = np.vstack([points, points[:1]])
    seg = np.hypot(*np.diff(closed, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    t = np.linspace(0.0, s[-1], n, endpoint=False)
    return np.column_stack([np.interp(t, s, closed[:, 0]), np.interp(t, s, closed[:, 1])])


def smooth_contour(
    points: np.ndarray,
    *,
    method: SmoothMethod = "efd",
    harmonics: int = 20,
    spline_s: float | None = None,
    n_points: int = 2000,
) -> np.ndarray:
    """Smooth a closed contour and resample it evenly in arc length.

    `efd`: elliptic Fourier reconstruction with `harmonics` terms (fewer = smoother).
    `spline`: periodic smoothing spline; `spline_s` defaults to one px² per point.
    """
    pts = resample_closed(points, max(n_points, len(points)))
    if method == "efd":
        import pyefd

        coeffs = pyefd.elliptic_fourier_descriptors(pts, order=harmonics)
        locus = pyefd.calculate_dc_coefficients(pts)
        dense = pyefd.reconstruct_contour(coeffs, locus=locus, num_points=4 * n_points + 1)[:-1]
    elif method == "spline":
        from scipy.interpolate import splev, splprep

        closed = np.vstack([pts, pts[:1]])
        s = float(len(pts)) if spline_s is None else spline_s
        tck, _ = splprep(closed.T, s=s, per=1)
        dense = np.column_stack(splev(np.linspace(0, 1, 4 * n_points, endpoint=False), tck))
    else:
        raise ProfileError(f"unknown smoothing method {method!r}")
    return resample_closed(dense, n_points)


def anchor_tips(smooth: np.ndarray, raw: np.ndarray) -> np.ndarray:
    """Stretch the smoothed outline in x so its x extremes match the traced contour's.

    Fourier and spline smoothing both round off pointed tips, which shortens the body by
    ~1% at typical strengths. Each half (nose side, tail side of the midpoint) is stretched
    linearly so the tips land back on the traced extremes; depths are untouched.
    """
    s_lo, s_hi = smooth[:, 0].min(), smooth[:, 0].max()
    r_lo, r_hi = raw[:, 0].min(), raw[:, 0].max()
    mid = 0.5 * (s_lo + s_hi)
    x = np.interp(smooth[:, 0], [s_lo, mid, s_hi], [r_lo, mid, r_hi])
    return np.column_stack([x, smooth[:, 1]])


@dataclass(frozen=True)
class BodyFrame:
    """Maps pixel coordinates to the body frame: x from the nose tip, z up, origin at the nose."""

    origin_px: tuple[float, float]
    direction: int  # +1 when the nose faces left (x grows with pixel x), -1 when it faces right
    mm_per_px: float = 1.0

    def to_body(self, xy_px: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy_px, dtype=float)
        x = (xy[..., 0] - self.origin_px[0]) * self.direction * self.mm_per_px
        z = (self.origin_px[1] - xy[..., 1]) * self.mm_per_px
        return np.stack([x, z], axis=-1)

    def to_px(self, xz: np.ndarray) -> np.ndarray:
        xz = np.asarray(xz, dtype=float)
        x = self.origin_px[0] + self.direction * xz[..., 0] / self.mm_per_px
        y = self.origin_px[1] - xz[..., 1] / self.mm_per_px
        return np.stack([x, y], axis=-1)

    def with_scale(self, mm_per_px: float) -> "BodyFrame":
        return BodyFrame(self.origin_px, self.direction, mm_per_px)


def body_frame(points_px: np.ndarray, nose: NoseSide = "left") -> BodyFrame:
    """Frame whose origin is the nose tip: the contour's extreme-x point on the `nose` side."""
    i = int(np.argmin(points_px[:, 0]) if nose == "left" else np.argmax(points_px[:, 0]))
    return BodyFrame((float(points_px[i, 0]), float(points_px[i, 1])), 1 if nose == "left" else -1)


@dataclass(frozen=True)
class Rails:
    """Dorsal (`top`) and ventral (`bottom`) outlines sampled on interior x positions.

    Both are single-valued in x. The tips are separate points where the rails meet.
    """

    nose: tuple[float, float]
    tail: tuple[float, float]
    x: np.ndarray
    top: np.ndarray
    bottom: np.ndarray

    @property
    def length(self) -> float:
        return self.tail[0] - self.nose[0]

    @property
    def max_depth(self) -> float:
        return float((self.top - self.bottom).max())

    def at(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Interpolated (top, bottom) at x, closing to the tip points at the ends."""
        xs = np.concatenate([[self.nose[0]], self.x, [self.tail[0]]])
        top = np.concatenate([[self.nose[1]], self.top, [self.tail[1]]])
        bot = np.concatenate([[self.nose[1]], self.bottom, [self.tail[1]]])
        return np.interp(x, xs, top), np.interp(x, xs, bot)


def split_rails(points: np.ndarray, n: int = 1000) -> Rails:
    """Split a closed body-frame contour at its x extremes into dorsal and ventral rails.

    At each of `n` interior x positions the rails are the highest and lowest crossings of the
    outline, so they are single-valued in x even if the outline folds back on itself.
    """
    i_nose, i_tail = int(np.argmin(points[:, 0])), int(np.argmax(points[:, 0]))
    nose, tail = points[i_nose], points[i_tail]
    if tail[0] - nose[0] <= 0:
        raise ProfileError("degenerate contour: zero length")
    gx = np.linspace(nose[0], tail[0], n + 2)[1:-1]

    p0 = points
    p1 = np.roll(points, -1, axis=0)
    x0, z0, x1, z1 = p0[:, 0:1], p0[:, 1:2], p1[:, 0:1], p1[:, 1:2]
    spans = ((x0 <= gx) & (gx < x1)) | ((x1 <= gx) & (gx < x0))
    with np.errstate(divide="ignore", invalid="ignore"):
        z = z0 + (gx - x0) * (z1 - z0) / (x1 - x0)
    z = np.where(spans, z, np.nan)
    top, bottom = np.nanmax(z, axis=0), np.nanmin(z, axis=0)
    return Rails(
        (float(nose[0]), float(nose[1])), (float(tail[0]), float(tail[1])), gx, top, bottom
    )
