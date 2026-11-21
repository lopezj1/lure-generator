"""Paper-sheet scale: a sheet of known size under the subject gives mm/px and tilt correction.

The subject (a lure on its side, or a drawn or printed profile) lies on a plain sheet of
paper that sits on a darker surface, with the whole sheet in frame. The sheet's four corners
give a homography to a fronto-parallel view of the paper plane, like the ArUco mode but
without a printed marker.

Detection runs in two steps:

1. **Coarse quad** on a downscaled image: Otsu (and a few stricter) thresholds of the gray
   image, the convex hull of the largest bright region (which bridges the subject where it
   overlaps an edge) and `approxPolyDP` down to four corners.
2. **Edge refinement** on the full image: each side is sampled along its outward normal,
   the sub-pixel position of the strongest bright → dark step is located at each sample, a
   line is fitted with outlier rejection (where the subject or a shadow crosses the edge)
   and adjacent lines are intersected.

The sheet's aspect ratio is then estimated from the corners alone (pinhole camera, square
pixels, principal point at the image center; Zhang & He, "Whiteboard scanning and image
enhancement", 2007). It decides portrait vs landscape and its deviation from the nominal
ratio is reported as the residual.
"""

import math

import cv2
import numpy as np

from luregen.config.models import PAPER_SIZES_MM as SHEETS  # short × long side, mm
from luregen.profile.extract import ProfileError, to_gray

DETECT_MAX_SIDE = 1000  # px; the coarse quad is found on an image downscaled to this
MIN_AREA_FRAC = 0.04  # the sheet must cover at least this fraction of the frame
WARN_ASPECT_ERROR = 0.02
MAX_ASPECT_ERROR = 0.15
MARGIN_FRAC = 0.04  # rectified crop: the sheet plus this fraction of its long side


def parse_sheet(spec: str) -> tuple[str, tuple[float, float]]:
    """`letter`, `a4` or `<W>x<H>` (mm) → (name, (short, long) side in mm)."""
    key = spec.strip().lower()
    if key in SHEETS:
        return key, SHEETS[key]
    try:
        w, h = (float(v) for v in key.split("x"))
    except ValueError as exc:
        raise ProfileError(
            f"unknown sheet {spec!r}: use letter, a4 or <width>x<height> in mm"
        ) from exc
    if w <= 0 or h <= 0:
        raise ProfileError(f"sheet size must be positive, got {spec!r}")
    return f"{w:g}x{h:g} mm", (min(w, h), max(w, h))


def order_corners(quad: np.ndarray) -> np.ndarray:
    """Corners in image order TL, TR, BR, BL (clockwise on screen, y down)."""
    q = np.asarray(quad, dtype=float).reshape(4, 2)
    c = q.mean(axis=0)
    q = q[np.argsort(np.arctan2(q[:, 1] - c[1], q[:, 0] - c[0]))]
    start = int(np.argmin(q.sum(axis=1)))
    return np.roll(q, -start, axis=0)


def _touches_border(points: np.ndarray, shape: tuple[int, ...], margin: float) -> bool:
    h, w = shape[:2]
    x, y = points[:, 0], points[:, 1]
    return bool(np.any((x < margin) | (y < margin) | (x > w - 1 - margin) | (y > h - 1 - margin)))


def _line_corners(poly: np.ndarray) -> np.ndarray | None:
    """Corners from the four longest sides of a convex polygon, extended to meet.

    A corner covered by the subject or cut off by a shadow becomes a short extra side of the
    hull; the four sheet sides are the long ones, and their intersections restore the corner.
    """
    nxt = np.roll(poly, -1, axis=0)
    lengths = np.hypot(*(nxt - poly).T)
    sides = np.sort(np.argsort(lengths)[-4:])
    corners = []
    for k in range(4):
        i, j = sides[k - 1], sides[k]
        p0, d0 = poly[i], nxt[i] - poly[i]
        p1, d1 = poly[j], nxt[j] - poly[j]
        m = np.column_stack([d0, -d1])
        if abs(np.linalg.det(m)) < 1e-6 * lengths[i] * lengths[j]:
            return None
        s = np.linalg.solve(m, p1 - p0)
        corners.append(p0 + s[0] * d0)
    return np.array(corners, dtype=float)


def _quad_from_mask(bright: np.ndarray) -> np.ndarray | None:
    h, w = bright.shape
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:3]:
        area = cv2.contourArea(contour)
        if area < MIN_AREA_FRAC * h * w:
            break
        if _touches_border(contour.reshape(-1, 2).astype(float), bright.shape, 1.0):
            continue  # the sheet must lie fully inside the frame
        hull = cv2.convexHull(contour)
        poly = cv2.approxPolyDP(hull, 0.01 * cv2.arcLength(hull, True), True)
        if len(poly) < 4:
            continue
        quad = _line_corners(poly.reshape(-1, 2).astype(float))
        if quad is None or not cv2.isContourConvex(quad.astype(np.float32).reshape(-1, 1, 2)):
            continue
        if area < 0.8 * cv2.contourArea(quad.astype(np.float32)):
            continue  # bright region does not fill the quad: not a sheet
        if _touches_border(quad, bright.shape, 2.0):
            continue
        return quad
    return None


def coarse_quad(gray: np.ndarray) -> np.ndarray | None:
    """Rough sheet corners (image px, TL TR BR BL) or None, from a downscaled copy."""
    h, w = gray.shape
    f = max(1.0, max(h, w) / DETECT_MAX_SIDE)
    small = cv2.resize(gray, (round(w / f), round(h / f)), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (5, 5), 0)
    otsu, _ = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    # Otsu first; stricter thresholds split a sheet from light patches of the surface.
    for frac in (0.0, 0.3, 0.55):
        level = otsu + frac * (255 - otsu)
        bright = (small > level).astype(np.uint8) * 255
        bright = cv2.morphologyEx(bright, cv2.MORPH_OPEN, k)
        quad = _quad_from_mask(bright)
        if quad is not None:
            return order_corners((quad + 0.5) * f - 0.5)
    return None


def _fit_line(points: np.ndarray, tol: float) -> tuple[np.ndarray, np.ndarray, int]:
    """Robust line (point, unit direction, inlier count): RANSAC seed, then least squares."""
    rng = np.random.default_rng(0)
    n = len(points)
    best = np.zeros(n, dtype=bool)
    for _ in range(min(200, n * (n - 1) // 2)):
        i, j = rng.choice(n, 2, replace=False)
        d = points[j] - points[i]
        norm = np.hypot(*d)
        if norm < 1e-9:
            continue
        normal = np.array([-d[1], d[0]]) / norm
        inliers = np.abs((points - points[i]) @ normal) < tol
        if inliers.sum() > best.sum():
            best = inliers
    keep = best
    for _ in range(5):
        mean = points[keep].mean(axis=0)
        direction = np.linalg.svd(points[keep] - mean)[2][0]
        normal = np.array([-direction[1], direction[0]])
        resid = np.abs((points - mean) @ normal)
        sigma = 1.4826 * np.median(resid[keep])
        new = resid < max(0.5, 3.0 * sigma)
        if np.array_equal(new, keep) or new.sum() < 2:
            break
        keep = new
    return mean, direction, int(keep.sum())


def refine_quad(
    gray: np.ndarray, quad: np.ndarray, reach: float, samples: int = 80
) -> np.ndarray | None:
    """Sub-pixel corners from line fits to the four bright → dark edges, or None."""
    from scipy.ndimage import map_coordinates

    img = cv2.GaussianBlur(gray, (0, 0), 1.0).astype(np.float32)
    center = quad.mean(axis=0)
    step = 0.5
    offsets = np.arange(-reach, reach + 1e-9, step)
    lines = []
    for a, b in zip(quad, np.roll(quad, -1, axis=0), strict=True):
        t = (b - a) / np.linalg.norm(b - a)
        n = np.array([t[1], -t[0]])
        if np.dot(n, (a + b) / 2 - center) < 0:
            n = -n  # outward
        base = a + np.outer(np.linspace(0.1, 0.9, samples), b - a)
        pts = base[:, None, :] + offsets[None, :, None] * n
        prof = map_coordinates(img, [pts[..., 1], pts[..., 0]], order=1, mode="nearest")
        d = np.diff(prof, axis=1)  # bright paper → darker surface is a negative step
        i = np.argmin(d, axis=1)
        rows = np.arange(samples)
        strength = -d[rows, i]
        ok = strength > max(1.0, 0.35 * np.median(strength))
        rows, i = rows[ok], i[ok]
        # Sub-pixel edge: centroid of the step's gradient within ±2 px of its peak.
        half = int(round(2.0 / step))
        idx = np.clip(i[:, None] + np.arange(-half, half + 1)[None, :], 0, d.shape[1] - 1)
        g = np.clip(-d[rows[:, None], idx], 0, None)
        mid = offsets[idx] + step / 2
        pos = (g * mid).sum(axis=1) / np.maximum(g.sum(axis=1), 1e-9)
        edge = base[rows] + pos[:, None] * n
        if len(edge) < samples // 3:
            return None
        mean, direction, count = _fit_line(edge, tol=1.0)
        if count < samples // 3:
            return None
        lines.append((mean, direction))

    corners = []
    for k, (p1, d1) in enumerate(lines):
        p0, d0 = lines[k - 1]  # corner k is where side k-1 (ending there) meets side k
        m = np.column_stack([d0, -d1])
        if abs(np.linalg.det(m)) < 1e-6:
            return None
        s = np.linalg.solve(m, p1 - p0)
        corners.append(p0 + s[0] * d0)
    return np.array(corners, dtype=float)


def detect_sheet(image: np.ndarray) -> np.ndarray | None:
    """Sheet corners (4, 2) in image px, ordered TL, TR, BR, BL, or None if no sheet."""
    gray = to_gray(image)
    quad = coarse_quad(gray)
    if quad is None:
        return None
    f = max(1.0, max(gray.shape) / DETECT_MAX_SIDE)
    side = float(np.hypot(*(np.roll(quad, -1, axis=0) - quad).T).min())
    reach = max(8.0, 4.0 * f + 0.02 * side)
    refined = refine_quad(gray, quad, reach)
    if refined is None or np.abs(refined - quad).max() > 2 * reach:
        return None
    again = refine_quad(gray, refined, max(4.0, 1.5 * f))  # tighter window, less clutter
    if again is not None and np.abs(again - refined).max() < 2 * reach:
        refined = again
    if _touches_border(refined, gray.shape, 0.0):
        return None
    return order_corners(refined)


def _aspect_once(corners: np.ndarray, shape: tuple[int, ...]) -> tuple[float, bool]:
    h, w = shape[:2]
    pp = np.array([(w - 1) / 2, (h - 1) / 2])
    tl, tr, br, bl = (np.append(p - pp, 1.0) for p in corners)
    m1, m2, m3, m4 = tl, tr, bl, br  # rectangle (0,0), (w,0), (0,h), (w,h)
    k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
    k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    n2 = k2 * m2 - m1
    n3 = k3 * m3 - m1
    diag = math.hypot(w, h)

    def aspect(f2: float) -> float:
        num = (n2[0] ** 2 + n2[1] ** 2) / f2 + n2[2] ** 2
        den = (n3[0] ** 2 + n3[1] ** 2) / f2 + n3[2] ** 2
        return math.sqrt(num / den)

    if abs(n2[2] * n3[2]) > 1e-15:
        f2 = -(n2[0] * n3[0] + n2[1] * n3[1]) / (n2[2] * n3[2])
        if (0.3 * diag) ** 2 < f2 < (10 * diag) ** 2:
            return aspect(f2), True
    # Focal length not observable: a nominal one (≈ a phone's main camera), reliable only
    # if the result barely depends on it (near fronto-parallel shots).
    spread = abs(aspect((0.5 * diag) ** 2) / aspect((3 * diag) ** 2) - 1)
    return aspect(diag**2), spread < 0.01


def estimate_aspect(corners: np.ndarray, shape: tuple[int, ...]) -> tuple[float, bool]:
    """Estimated true width/height (TL→TR over TL→BL) of the rectangle imaged as `corners`.

    Pinhole camera with square pixels and the principal point at the image center. The
    focal length is solved from the corners when both vanishing points are well defined;
    otherwise a nominal one is used. Returns the estimate and whether it is reliable: it
    must hold up when the corners are jittered by half a pixel, and must not hinge on the
    nominal focal length (a side parallel to the image plane, e.g. tilt about one axis,
    leaves the focal length and with it the aspect ratio undetermined).
    """
    est, ok = _aspect_once(corners, shape)
    rng = np.random.default_rng(0)
    for _ in range(12):
        a, _ = _aspect_once(corners + rng.normal(0, 0.5, corners.shape), shape)
        ok = ok and abs(a / est - 1) < 0.01
    return est, ok


def sheet_target(
    corners: np.ndarray, sheet_mm: tuple[float, float], shape: tuple[int, ...]
) -> tuple[np.ndarray, tuple[float, float], float, float | None]:
    """Target rectangle for `corners` and how the sheet lies.

    Returns the target corners (same order, same center and in-image rotation, the photo's
    average resolution), the sheet size along the image's (horizontal, vertical) sides in
    mm, px per mm and the aspect residual (estimated / nominal − 1, None if not observable).
    """
    short, long = sheet_mm
    est, reliable = estimate_aspect(corners, shape)
    if est >= 1.0:
        size = (long, short)  # landscape: the long side runs along TL→TR
        error = est / (long / short) - 1
    else:
        size = (short, long)
        error = (1 / est) / (long / short) - 1
    if not reliable:
        error = None
    area = cv2.contourArea(corners.astype(np.float32))
    k = math.sqrt(area / (size[0] * size[1]))
    top, bottom = corners[1] - corners[0], corners[2] - corners[3]
    edge = top / np.linalg.norm(top) + bottom / np.linalg.norm(bottom)
    angle = math.atan2(edge[1], edge[0])
    half = np.array([size[0], size[1]]) * k / 2
    unit = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) * half
    rot = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    return unit @ rot.T + corners.mean(axis=0), size, k, error


def inset_polygon(sheet_px: np.ndarray, inset_px: float) -> np.ndarray:
    """A rectangle's corners (TL, TR, BR, BL) moved `inset_px` inward along both sides."""
    out = []
    for i, c in enumerate(sheet_px):
        u = sheet_px[i - 1] - c
        v = sheet_px[(i + 1) % 4] - c
        out.append(c + inset_px * (u / np.linalg.norm(u) + v / np.linalg.norm(v)))
    return np.array(out)


def outside_filled(image: np.ndarray, polygon: np.ndarray) -> np.ndarray:
    """Copy of `image` with everything outside `polygon` set to the median color inside it."""
    inside = np.zeros(image.shape[:2], dtype=np.uint8)
    cv2.fillPoly(inside, [np.round(polygon).astype(np.int32)], 255)
    if not inside.any():
        raise ProfileError("sheet interior is empty (inset larger than the sheet?)")
    out = image.copy()
    fill = np.median(image[inside > 0], axis=0)
    out[inside == 0] = np.round(fill).astype(image.dtype)
    return out


def check_sheet(error: float | None, name: str) -> None:
    if error is not None and abs(error) > MAX_ASPECT_ERROR:
        raise ProfileError(
            f"the bright quadrilateral found is not a {name} sheet (aspect off by "
            f"{100 * error:+.0f}%); check the sheet size, keep the whole sheet in frame and "
            "use a darker, plain surface under it"
        )
