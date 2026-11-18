"""Image scale: ArUco marker homography (corrects tilt, gives mm/px) or a known total length.

The marker must lie in the same plane as the lure's profile (flat on the table next to it
for a side view shot from above). Its printed size is the side of the black square.
"""

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from luregen.profile.extract import ProfileError

ARUCO_DICT = cv2.aruco.DICT_4X4_50
MAX_CANVAS_FACTOR = 3.0  # rectified image may be at most this many times the input's size

ScaleMode = Literal["aruco", "length", "auto"]


@dataclass(frozen=True)
class Scale:
    method: Literal["aruco", "length"]
    mm_per_px: float
    homography: np.ndarray | None = None  # input px → rectified px
    marker_px: np.ndarray | None = None  # (4, 2) marker corners in the rectified image
    marker_mm: float | None = None

    def describe(self) -> str:
        if self.method == "aruco":
            return f"ArUco {self.marker_mm:g} mm marker, {self.mm_per_px:.4f} mm/px"
        return f"known total length, {self.mm_per_px:.4f} mm/px"


def detect_marker(image: np.ndarray) -> np.ndarray | None:
    """Corners (4, 2) of the largest DICT_4X4_50 marker, in its own TL, TR, BR, BL order."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(ARUCO_DICT), params)
    corners, ids, _ = detector.detectMarkers(gray)
    if ids is None or not corners:
        return None
    quads = [c.reshape(4, 2).astype(float) for c in corners]
    quad = max(quads, key=lambda q: cv2.contourArea(q.astype(np.float32)))
    return refine_corners(gray, quad)


def refine_corners(gray: np.ndarray, corners: np.ndarray, samples: int = 48) -> np.ndarray:
    """Sharpen marker corners by fitting a line to each outer edge and intersecting them.

    The detector's corners are good to a few tenths of a pixel, and the homography amplifies
    that error across a lure several marker-widths long. Each edge is instead located at
    `samples` points by its sub-pixel half-intensity crossing (black border → white quiet
    zone), and a least-squares line through those points is far steadier than one corner.
    """
    from scipy.ndimage import map_coordinates

    img = gray.astype(float)
    center = corners.mean(axis=0)
    side = float(np.hypot(*(np.roll(corners, -1, axis=0) - corners).T).mean())
    reach = max(2.0, side / 14)  # stay inside the outer black border (1/6 of the side)
    offsets = np.arange(-reach, reach + 1e-9, 0.25)
    lines = []
    for a, b in zip(corners, np.roll(corners, -1, axis=0), strict=True):
        t = (b - a) / np.linalg.norm(b - a)
        n = np.array([t[1], -t[0]])
        if np.dot(n, (a + b) / 2 - center) < 0:
            n = -n
        base = a + np.outer(np.linspace(0.15, 0.85, samples), b - a)
        pts = base[:, None, :] + offsets[None, :, None] * n  # (samples, offsets, 2)
        prof = map_coordinates(img, [pts[..., 1], pts[..., 0]], order=1, mode="nearest")
        edge = []
        for row, p in zip(prof, pts, strict=True):
            level = 0.5 * (row.min() + row.max())
            above = np.nonzero(row >= level)[0]
            if row.max() - row.min() < 20 or len(above) == 0 or above[0] == 0:
                continue
            i = above[0]
            f = (level - row[i - 1]) / (row[i] - row[i - 1])
            edge.append(p[i - 1] + f * (p[i] - p[i - 1]))
        if len(edge) < samples // 3:
            return corners  # low contrast: keep the detector's corners
        edge = np.array(edge)
        mean = edge.mean(axis=0)
        direction = np.linalg.svd(edge - mean)[2][0]
        lines.append((mean, direction))

    refined = []
    for i, (p1, d1) in enumerate(lines):
        p0, d0 = lines[i - 1]  # corner i is where edge i-1 (ending there) meets edge i
        s = np.linalg.solve(np.column_stack([d0, -d1]), p1 - p0)
        refined.append(p0 + s[0] * d0)
    refined = np.array(refined, dtype=float)
    if np.abs(refined - corners).max() > 2.0:
        return corners  # refinement went astray; trust the detector
    return refined


def _upright_square(corners: np.ndarray) -> np.ndarray:
    """Square target for `corners` with the same center, mean side and in-image rotation.

    Keeping the marker's apparent rotation means rectification removes tilt without turning
    the photo, so the image's horizontal stays the lure's horizontal whatever way the marker
    was printed or placed.
    """
    sides = np.hypot(*(np.roll(corners, -1, axis=0) - corners).T)
    side = float(sides.mean())
    top, bottom = corners[1] - corners[0], corners[2] - corners[3]
    edge = top / np.linalg.norm(top) + bottom / np.linalg.norm(bottom)
    angle = np.arctan2(edge[1], edge[0])
    unit = np.array([[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]]) * side
    rot = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    return unit @ rot.T + corners.mean(axis=0)


def rectify(image: np.ndarray, corners: np.ndarray, marker_mm: float) -> tuple[np.ndarray, Scale]:
    """Warp `image` so the marker plane is fronto-parallel; returns the image and its scale."""
    target = _upright_square(corners)
    h_mat = cv2.getPerspectiveTransform(corners.astype(np.float32), target.astype(np.float32))

    h, w = image.shape[:2]
    frame = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64).reshape(-1, 1, 2)
    hom = np.hstack([frame.reshape(-1, 2), np.ones((4, 1))]) @ h_mat.T
    if np.any(hom[:, 2] <= 0):
        raise ProfileError("marker tilt is too extreme to rectify (horizon inside the image)")
    warped = cv2.perspectiveTransform(frame, h_mat).reshape(-1, 2)
    lo, hi = np.floor(warped.min(axis=0)), np.ceil(warped.max(axis=0))
    size = hi - lo
    if size[0] * size[1] > MAX_CANVAS_FACTOR**2 * w * h:
        raise ProfileError("marker tilt is too extreme to rectify (output would be too large)")

    shift = np.array([[1, 0, -lo[0]], [0, 1, -lo[1]], [0, 0, 1]], dtype=np.float64)
    h_mat = shift @ h_mat
    out = cv2.warpPerspective(
        image, h_mat, (int(size[0]), int(size[1])), borderMode=cv2.BORDER_REPLICATE
    )
    marker_px = target - lo
    side = float(np.hypot(*(np.roll(marker_px, -1, axis=0) - marker_px).T).mean())
    return out, Scale("aruco", marker_mm / side, h_mat, marker_px, marker_mm)


def length_scale(length_px: float, length_mm: float) -> Scale:
    """Scale from the silhouette's nose-to-tail length in pixels and the known length in mm."""
    if length_px <= 0 or length_mm <= 0:
        raise ProfileError("length scale needs a positive length")
    return Scale("length", length_mm / length_px)
