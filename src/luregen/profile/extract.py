"""Silhouette extraction: image → mask → contour → smoothed outline → dorsal/ventral rails.

Pixel coordinates are OpenCV's (x right, y down). `body_frame` maps a contour into the
package frame (x from the nose tip toward the tail, z up, nose tip at the origin) so rails
and stations never deal with image orientation.
"""

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
