"""Synthetic test images for profile extraction: silhouettes, ArUco markers, camera tilt.

Shapes are drawn at SS× resolution and area-averaged down, so edges are anti-aliased like a
real photo. Scene coordinates are mm with x right and z up; `PX_PER_MM` sets the resolution.
"""

import cv2
import numpy as np

PX_PER_MM = 4.0
SS = 4  # supersampling factor
BG = 235
BODY = 60


class Scene:
    """A flat scene (the marker plane) of `w_mm` × `h_mm`, background `BG`."""

    def __init__(self, w_mm: float, h_mm: float, px_per_mm: float = PX_PER_MM) -> None:
        self.k = px_per_mm * SS
        self.big = np.full((round(h_mm * self.k), round(w_mm * self.k)), BG, dtype=np.uint8)

    def _px(self, xz: np.ndarray) -> np.ndarray:
        """Scene mm (x right, z up from the bottom-left corner) → supersampled px."""
        h = self.big.shape[0]
        xz = np.asarray(xz, dtype=float)
        return np.column_stack([xz[:, 0] * self.k, h - xz[:, 1] * self.k])

    def polygon(self, xz: np.ndarray, value: int = BODY) -> None:
        pts = np.round(self._px(xz) * 16).astype(np.int32)
        cv2.fillPoly(self.big, [pts], value, lineType=cv2.LINE_AA, shift=4)

    def ellipse(self, cx: float, cz: float, a: float, b: float, value: int = BODY) -> None:
        t = np.linspace(0, 2 * np.pi, 4000, endpoint=False)
        self.polygon(np.column_stack([cx + a * np.cos(t), cz + b * np.sin(t)]), value)

    def marker(self, x: float, z: float, side_mm: float, marker_id: int = 7) -> None:
        """ArUco DICT_4X4_50 marker with its top-left corner at (x, z), on a white quiet zone."""
        side = round(side_mm * self.k)
        quiet = side // 4
        img = cv2.aruco.generateImageMarker(
            cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), marker_id, side
        )
        c, r = self._px(np.array([[x, z]]))[0].round().astype(int)
        self.big[r - quiet : r + side + quiet, c - quiet : c + side + quiet] = 255
        self.big[r : r + side, c : c + side] = img

    def render(self) -> np.ndarray:
        h, w = self.big.shape
        small = cv2.resize(self.big, (w // SS, h // SS), interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)


def tilt_matrix(shape: tuple[int, ...], amount: float) -> np.ndarray:
    """Homography that keystones an image as if shot from an oblique camera."""
    h, w = shape[:2]
    d = amount
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[w * d, h * d * 0.5], [w * (1 - d * 0.3), 0], [w, h * (1 - d * 0.2)], [0, h]])
    return cv2.getPerspectiveTransform(src, dst)


def tilt(image: np.ndarray, amount: float = 0.12) -> np.ndarray:
    """Perspective-distort an image by `tilt_matrix`."""
    h, w = image.shape[:2]
    m = tilt_matrix(image.shape, amount)
    return cv2.warpPerspective(image, m, (w, h), borderValue=(BG, BG, BG))


def lure_outline(length: float, depth: float, n: int = 400) -> np.ndarray:
    """Pointed-nose, pointed-tail body outline in body coordinates (nose at the origin)."""
    u = np.linspace(0, 1, n)
    shape = u**0.55 * (1 - u) ** 0.8
    shape /= shape.max()
    top = 0.6 * depth * shape
    bot = -0.4 * depth * shape
    x = u * length
    return np.vstack([np.column_stack([x, top]), np.column_stack([x, bot])[::-1][1:-1]])
