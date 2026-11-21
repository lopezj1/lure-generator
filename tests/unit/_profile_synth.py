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


# ---- paper-sheet photos ---------------------------------------------------------------------

LETTER = (215.9, 279.4)
PLANE_PX_PER_MM = 4.0  # resolution of the flat scene before the camera warp


def _rot(axis: str, deg: float) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    return {
        "x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]),
        "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]),
        "z": np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]),
    }[axis]


def camera_homography(
    size: tuple[int, int],
    center_mm: tuple[float, float],
    *,
    tilt: tuple[float, float] = (0.0, 0.0),
    roll: float = 0.0,
    distance: float = 420.0,
    focal: float = 1120.0,
) -> np.ndarray:
    """Plane mm (x right, y down) → image px for a pinhole camera above the plane.

    Square pixels, principal point at the image center; `tilt` rotates the camera about the
    plane's x and y axes (degrees), `roll` about the optical axis.
    """
    w, h = size
    k = np.array([[focal, 0, (w - 1) / 2], [0, focal, (h - 1) / 2], [0, 0, 1]])
    r = _rot("z", roll) @ _rot("x", tilt[0]) @ _rot("y", tilt[1])
    shift = np.array([[1, 0, -center_mm[0]], [0, 1, -center_mm[1]], [0, 0, 1]])
    return k @ np.column_stack([r[:, 0], r[:, 1], [0, 0, distance]]) @ shift


def paper_photo(
    *,
    sheet: tuple[float, float] = LETTER,
    landscape: bool = True,
    with_sheet: bool = True,
    lure: tuple[float, float] | None = (175.0, 50.0),
    blobs: list[tuple[float, float, float, float]] = (),
    tilt: tuple[float, float] = (12.0, -8.0),
    roll: float = 0.0,
    distance: float = 420.0,
    size: tuple[int, int] = (1280, 960),
    focal: float = 1120.0,
    seed: int = 0,
) -> tuple[np.ndarray, dict]:
    """A sheet on a wood-like surface with a dark lure on it, shot by a tilted camera.

    `lure` is (length, depth) in mm, nose left, centered on the sheet (which is left out
    when not `with_sheet`). `blobs` are extra dark
    ellipses (cx, cy, a, b in plane mm, y down), e.g. overlapping an edge. Returns the BGR
    photo and the truth: sheet corners in image px (TL, TR, BR, BL on the plane) and the
    plane → image homography.
    """
    rng = np.random.default_rng(seed)
    sw, sh = (sheet[1], sheet[0]) if landscape else sheet
    pw, ph = sw + 160, sh + 140  # plane extent, mm
    k = PLANE_PX_PER_MM
    yy = np.arange(round(ph * k), dtype=np.float32)[:, None]
    xx = np.arange(round(pw * k), dtype=np.float32)[None, :]
    grain = np.sin(yy / k * 0.9 + 3 * np.sin(xx / k * 0.02)) * 10 + np.sin(yy / k * 0.23) * 14
    wood = grain[..., None] + np.array([55, 100, 150], dtype=np.float32)  # BGR, brown
    wood += rng.standard_normal(wood.shape, dtype=np.float32) * 4
    plane = np.clip(wood, 0, 255).astype(np.uint8)

    x0, y0 = (pw - sw) / 2, (ph - sh) / 2
    corners_mm = np.array([[x0, y0], [x0 + sw, y0], [x0 + sw, y0 + sh], [x0, y0 + sh]])

    def fill(poly_mm: np.ndarray, color) -> None:
        pts = np.round((poly_mm * k - 0.5) * 16).astype(np.int32)
        cv2.fillPoly(plane, [pts], color, lineType=cv2.LINE_8, shift=4)

    # The sheet is drawn with exact area coverage: its edges are the reference under test.
    cx = with_sheet * np.clip(np.minimum(xx + 1, (x0 + sw) * k) - np.maximum(xx, x0 * k), 0, 1)
    cy = np.clip(np.minimum(yy + 1, (y0 + sh) * k) - np.maximum(yy, y0 * k), 0, 1)
    alpha = (cy * cx).astype(np.float32)[..., None]  # separable: exact for a rectangle
    paper = np.array([238, 240, 242], dtype=np.float32)
    plane = np.round(plane + alpha * (paper - plane)).astype(np.uint8)
    if lure is not None:
        length, depth = lure
        outline = lure_outline(length, depth)
        pts = np.column_stack([x0 + (sw - length) / 2 + outline[:, 0], ph / 2 - outline[:, 1]])
        fill(pts, (48, 52, 58))
    for cx, cy, a, b in blobs:
        t = np.linspace(0, 2 * np.pi, 400, endpoint=False)
        fill(np.column_stack([cx + a * np.cos(t), cy + b * np.sin(t)]), (45, 50, 55))

    # Camera: warp at 2× and area-average down so edges are anti-aliased like a photo.
    h_img = camera_homography(
        size, (pw / 2, ph / 2), tilt=tilt, roll=roll, distance=distance, focal=focal
    )
    to_plane_px = np.diag([1 / k, 1 / k, 1.0]) @ np.array([[1, 0, 0.5], [0, 1, 0.5], [0, 0, 1]])
    up = np.diag([2.0, 2.0, 1.0]) @ np.array([[1, 0, 0.5], [0, 1, 0.5], [0, 0, 1]])
    up[:2, 2] -= 0.5
    m = up @ h_img @ to_plane_px
    big = cv2.warpPerspective(
        plane, m, (2 * size[0], 2 * size[1]), flags=cv2.INTER_LINEAR, borderValue=(40, 70, 100)
    )
    img = cv2.resize(big, size, interpolation=cv2.INTER_AREA).astype(np.float32)

    # Lighting falloff across the frame and sensor noise.
    ramp = np.linspace(0.78, 1.04, size[0], dtype=np.float32)[None, :, None]
    img = img * ramp * np.linspace(1.02, 0.9, size[1], dtype=np.float32)[:, None, None]
    img += rng.standard_normal(img.shape, dtype=np.float32) * 2.5
    img = np.clip(img, 0, 255).astype(np.uint8)

    corners_px = cv2.perspectiveTransform(corners_mm.reshape(-1, 1, 2), h_img).reshape(-1, 2)
    return img, {"corners_px": corners_px, "plane_to_image": h_img, "sheet_mm": (sw, sh)}
