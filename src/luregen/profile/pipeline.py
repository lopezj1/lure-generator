"""End-to-end profile extraction: side (and optional top) image → `StationTable`."""

from dataclasses import dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np

from luregen.config.models import ProfileConfig
from luregen.profile.extract import (
    BodyFrame,
    NoseSide,
    ProfileError,
    Rails,
    SmoothMethod,
    anchor_tips,
    body_frame,
    largest_contour,
    segment,
    smooth_contour,
    split_rails,
)
from luregen.profile.scale import (
    Scale,
    ScaleMode,
    detect_marker,
    length_scale,
    rectify,
    rectify_sheet,
)
from luregen.profile.sheet import (
    SHEETS,
    WARN_ASPECT_ERROR,
    detect_sheet,
    inset_polygon,
    outside_filled,
)
from luregen.profile.stations import build_table, width_from_ratio, width_from_top
from luregen.profile.table import StationTable

MAX_MARKER_REACH = 4.0  # marker widths; beyond this, tilt correction extrapolates poorly


@dataclass(frozen=True)
class ExtractOptions:
    scale: ScaleMode = "auto"
    length_mm: float | None = None
    marker_mm: float = 30.0
    sheet: str = "letter"  # name for messages
    sheet_mm: tuple[float, float] = SHEETS["letter"]  # (short, long) side
    sheet_inset_mm: float = 4.0  # ignore this much of the sheet's border when segmenting
    nose: NoseSide = "left"
    smoothing: SmoothMethod = "efd"
    harmonics: int = 20
    spline_s: float | None = None
    use_rembg: bool = False


@dataclass(frozen=True)
class View:
    """One processed image, kept for the debug overlay."""

    image: np.ndarray  # working image (rectified when a marker was used), BGR
    mask: np.ndarray
    raw_px: np.ndarray  # traced contour, working-image px
    smooth_px: np.ndarray  # smoothed contour, working-image px
    frame: BodyFrame  # working px ↔ body mm
    scale: Scale
    rails: Rails  # body frame, mm
    notes: tuple[str, ...] = ()
    sheet_inner_px: np.ndarray | None = None  # sheet area used for segmentation (paper scale)


@dataclass(frozen=True)
class ProfileResult:
    table: StationTable
    side: View
    top: View | None = None
    notes: list[str] = field(default_factory=list)


def options_from_config(profile: ProfileConfig, length_mm: float | None = None) -> ExtractOptions:
    """Extraction options from a config `[profile]` section (`length_mm` from `size`)."""
    return ExtractOptions(
        scale=profile.scale,
        length_mm=length_mm,
        marker_mm=profile.aruco_marker_mm,
        sheet=profile.paper.size
        if profile.paper.size != "custom"
        else "{:g}x{:g} mm".format(*profile.paper.dims_mm()),
        sheet_mm=profile.paper.dims_mm(),
        sheet_inset_mm=profile.paper.inset_mm,
        nose=profile.nose,
        smoothing=profile.smoothing.method,
        harmonics=profile.smoothing.harmonics,
        spline_s=profile.smoothing.spline_s,
    )


def read_image(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ProfileError(f"cannot read image {path}")
    return image


def _find_scale(image: np.ndarray, opts: ExtractOptions) -> tuple[np.ndarray, Scale | None]:
    """Rectify by a marker or a sheet (in that order for `auto`); None scale if neither."""
    if opts.scale in ("aruco", "auto"):
        corners = detect_marker(image)
        if corners is not None:
            return rectify(image, corners, opts.marker_mm)
        if opts.scale == "aruco":
            raise ProfileError("scale = aruco but no DICT_4X4_50 marker was found")
    if opts.scale in ("paper", "auto"):
        corners = detect_sheet(image)
        if corners is not None:
            try:
                return rectify_sheet(image, corners, opts.sheet_mm, opts.sheet)
            except ProfileError:
                if opts.scale == "paper" or opts.length_mm is None:
                    raise
        elif opts.scale == "paper":
            raise ProfileError(
                f"scale = paper but no {opts.sheet} sheet was found: keep the whole sheet in "
                "frame on a darker, plain surface with some of the surface visible around it"
            )
    return image, None


def process_view(image: np.ndarray, opts: ExtractOptions) -> View:
    """Scale/rectify, segment, trace, smooth and split one silhouette into body-frame rails."""
    image, scale = _find_scale(image, opts)
    exclude: list[np.ndarray] = []
    notes: list[str] = []
    inner: np.ndarray | None = None
    seg_image = image
    if scale is not None and scale.marker_px is not None:
        center = scale.marker_px.mean(axis=0)
        exclude.append(center + 1.4 * (scale.marker_px - center))  # marker plus its quiet zone
    if scale is not None and scale.sheet_px is not None:
        # The paper is the background: ignore the surface around it and the sheet's border.
        inner = inset_polygon(scale.sheet_px, opts.sheet_inset_mm / scale.mm_per_px)
        seg_image = outside_filled(image, inner)
        if scale.aspect_error is not None and abs(scale.aspect_error) > WARN_ASPECT_ERROR:
            notes.append(
                f"sheet aspect ratio is off by {100 * scale.aspect_error:+.1f}%: shoot from "
                "directly above, keep the sheet flat and check the sheet size"
            )

    mask = segment(seg_image, use_rembg=opts.use_rembg, exclude=exclude)
    if inner is not None:
        _check_inside(mask, inner, opts.sheet_inset_mm)
    raw = largest_contour(mask)
    smooth = smooth_contour(
        raw, method=opts.smoothing, harmonics=opts.harmonics, spline_s=opts.spline_s
    )
    smooth = anchor_tips(smooth, raw)
    frame = body_frame(smooth, opts.nose)

    if scale is None:
        if opts.length_mm is None:
            raise ProfileError(
                "no ArUco marker or paper sheet found; pass a known total length for scale"
            )
        length_px = float(frame.to_body(smooth)[:, 0].max())
        scale = length_scale(length_px, opts.length_mm)
    frame = frame.with_scale(scale.mm_per_px)
    rails = split_rails(frame.to_body(smooth))
    return View(image, mask, raw, smooth, frame, scale, rails, tuple(notes), inner)


def _check_inside(mask: np.ndarray, inner: np.ndarray, inset_mm: float) -> None:
    """The silhouette must not reach the (inset) sheet border, or it was clipped."""
    outside = np.full(mask.shape, 255, dtype=np.uint8)
    cv2.fillPoly(outside, [np.round(inner).astype(np.int32)], 0)
    outside = cv2.dilate(outside, np.ones((5, 5), np.uint8))
    if np.count_nonzero(mask & outside):
        raise ProfileError(
            f"the silhouette reaches the sheet's edge (within {inset_mm:g} mm): place the lure "
            "fully on the sheet, away from its edges"
        )


def extract_profile(
    image: Path | np.ndarray,
    opts: ExtractOptions = ExtractOptions(),
    *,
    n_stations: int = 23,
    top_image: Path | np.ndarray | None = None,
    width_ratio: float = 0.34,
) -> ProfileResult:
    """Side-profile image (+ optional top view) → station table and debug views."""
    side_img = read_image(image) if isinstance(image, Path) else image
    side = process_view(side_img, opts)
    meta = {
        "scale": side.scale.describe(),
        "smoothing": f"{opts.smoothing}"
        + (f" ({opts.harmonics} harmonics)" if opts.smoothing == "efd" else ""),
    }
    notes: list[str] = list(side.notes)
    if side.scale.marker_px is not None:
        marker = side.scale.marker_px
        reach = np.hypot(*(side.smooth_px - marker.mean(axis=0)).T).max()
        side_px = side.scale.marker_mm / side.scale.mm_per_px
        if reach > MAX_MARKER_REACH * side_px:
            notes.append(
                f"the body reaches {reach / side_px:.1f} marker widths from the marker; "
                "a larger marker or one placed next to the middle of the body is more accurate"
            )

    top: View | None = None
    if top_image is not None:
        top_img = read_image(top_image) if isinstance(top_image, Path) else top_image
        # Without its own marker, the top view is scaled to the side view's measured length.
        top_scale = "length" if opts.scale == "length" else "auto"
        top = process_view(top_img, replace(opts, scale=top_scale, length_mm=side.rails.length))
        notes += [f"top view: {n}" for n in top.notes]
        half_width = width_from_top(top.rails, side.rails.length)
        meta["width"] = f"top view ({top.scale.describe()})"
        mismatch = abs(top.rails.length / side.rails.length - 1)
        if top.scale.method in ("aruco", "paper") and mismatch > 0.02:
            notes.append(f"top-view length differs from side view by {100 * mismatch:.1f}%")
    else:
        half_width = width_from_ratio(width_ratio)
        meta["width"] = f"ratio_of_depth {width_ratio:g}"

    name = image.name if isinstance(image, Path) else "array"
    table = build_table(
        side.rails, n_stations, half_width, source=f"luregen profile {name}", meta=meta
    )
    return ProfileResult(table, side, top, notes)
