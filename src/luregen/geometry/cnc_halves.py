"""CNC (wood/board) halves: centerplane split, glue-face pockets and their 2D outlines (#40).

A CNC body is two halves glued on the centerplane y = 0. Everything a half needs from the
glue-face side is a flat-bottomed recess cut straight down from that face:

- **Glue-face pockets** (ballast, air): a rounded rectangle in the side view (x, z), `depth`
  into each half, so the closed pocket in the glued body is a rounded box 2·depth thick.
  Corner radius ≥ the pocket tool's radius, so the tool reaches every corner. Ballast is set
  into the pocket before glue-up.
- **Wire groove**: a ball-nose half-groove along the through-wire path (CAM only; too thin to
  change the mass properties).

`GlueFacePocket.solid()` is the build-time shape (build123d, imported lazily) that the glide
family subtracts for mass properties; the 2D/mesh helpers below (`split_halves`,
`silhouette_xz`, `glue_face_xz`) work on the exported section meshes and need only
trimesh/manifold3d/shapely, so `lure fab` never has to rebuild the CAD model.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import shapely
import shapely.ops
from shapely.geometry import Polygon
from shapely.geometry import box as shp_box

from luregen.geometry.validate import GeometryError
from luregen.profile.table import StationTable

QUAD_SEGS = 16  # arc resolution of shapely buffers (segments per quarter circle)
# The sizer reads the body from linearly interpolated stations; the smooth loft can sit
# ~0.1 mm inside that, so walls are checked with this much extra (F5 cnc.min_wall).
WALL_MARGIN_MM = 0.15


class CncGeometryError(GeometryError):
    pass


def tool_diameter_from_id(tool_id: str) -> float:
    """Diameter from a tool id named `<shape>_<diameter mm>` (as in dfm.cnc.tools)."""
    try:
        return float(tool_id.rsplit("_", 1)[1])
    except (IndexError, ValueError) as exc:
        raise CncGeometryError(f"tool id {tool_id!r} has no `_<diameter mm>` suffix") from exc


# --- glue-face pockets ------------------------------------------------------------------


@dataclass(frozen=True)
class GlueFacePocket:
    """Rounded rectangle centred at (x, z) in the side view, `depth` into each half."""

    x: float
    z: float
    length: float  # along x
    height: float  # along z
    depth: float  # per half (y)
    corner_r: float
    kind: str = "ballast"

    # Names shared with the print U-slot pocket, so report code can treat both alike.
    @property
    def z_axis(self) -> float:
        return self.z

    @property
    def r(self) -> float:
        return self.depth

    @property
    def volume(self) -> float:
        """Both halves together, mm³."""
        return 2 * self.depth * rounded_rect_area(self.length, self.height, self.corner_r)

    def footprint(self, grow: float = 0.0) -> Polygon:
        """Side-view (x, z) outline."""
        return rounded_rect(
            self.x, self.z, self.length + 2 * grow, self.height + 2 * grow, self.corner_r + grow
        )

    def solid(self, grow: float = 0.0):
        """The closed pocket in the glued body: y in [-depth, depth]."""
        from build123d import Axis, Box, Pos

        length, height = self.length + 2 * grow, self.height + 2 * grow
        r = min(self.corner_r + grow, 0.499 * min(length, height))
        box = Pos(self.x, 0, self.z) * Box(length, 2 * (self.depth + grow), height)
        return box.fillet(r, box.edges().filter_by(Axis.Y)) if r > 1e-3 else box

    def as_dict(self) -> dict:
        d = {k: round(v, 4) if isinstance(v, float) else v for k, v in asdict(self).items()}
        d["volume_cm3"] = round(self.volume / 1000.0, 4)
        return d


def rounded_rect_area(length: float, height: float, r: float) -> float:
    return length * height - (4 - math.pi) * r * r


def rounded_rect(x: float, z: float, length: float, height: float, r: float) -> Polygon:
    r = min(r, 0.4999 * min(length, height))
    core = shp_box(x - length / 2 + r, z - height / 2 + r, x + length / 2 - r, z + height / 2 - r)
    return core.buffer(r, quad_segs=QUAD_SEGS) if r > 0 else core


class _Body:
    """Fast side-view outline and half-width lookups from the station table."""

    def __init__(self, table: StationTable, shape: str):
        from luregen.geometry.envelope import section_profile

        self.xs = np.concatenate([[table.nose[0]], table.x, [table.tail[0]]])
        self.cz = np.concatenate([[table.nose[1]], table.center_z, [table.tail[1]]])
        self.v = np.concatenate([[0.0], table.v_radius, [0.0]])
        self.hw = np.concatenate([[0.0], table.half_width, [0.0]])
        yz = section_profile(shape, 2000)  # type: ignore[arg-type]
        right = yz[yz[:, 0] >= 0]
        order = np.argsort(right[:, 1])
        self._u, self._w = right[order, 1], right[order, 0]

    def at(self, x):
        return np.interp(x, self.xs, self.cz), np.interp(x, self.xs, self.v)

    def half_width(self, x, z):
        cz, v = self.at(x)
        hw = np.interp(x, self.xs, self.hw)
        with np.errstate(divide="ignore", invalid="ignore"):
            u = np.where(v > 0, (z - cz) / np.where(v > 0, v, 1), 9.0)
        w = np.interp(np.clip(u, -1, 1), self._u, self._w)
        return np.where(np.abs(u) < 1, hw * w, 0.0)


def _fits(body: _Body, p: GlueFacePocket, wall: float, x_min: float, x_max: float) -> bool:
    x0, x1 = p.x - p.length / 2, p.x + p.length / 2
    if x0 < x_min or x1 > x_max:
        return False
    xs = np.linspace(x0, x1, 25)
    cz, v = body.at(xs)
    z0, z1 = p.z - p.height / 2, p.z + p.height / 2
    if np.any(z0 < cz - v + wall) or np.any(z1 > cz + v - wall):
        return False
    return _max_depth(body, p.x, p.length, p.z, p.height, wall) >= p.depth - 1e-9


# The wall is the true (Euclidean) distance to the outer surface: around the pocket's
# top and bottom floor edges the body curves away, so the wall is checked on a quarter
# circle of radius `wall` about each edge, not only straight out in y (F5 cnc.min_wall).
_WALL_ANGLES = np.linspace(0.0, 0.5 * np.pi, 7)


def _max_depth(body: _Body, x: float, length: float, z: float, height: float, wall: float):
    wall = wall + WALL_MARGIN_MM
    xs = np.linspace(x - length / 2, x + length / 2, 25)
    out, dy = wall * np.sin(_WALL_ANGLES), wall * np.cos(_WALL_ANGLES)
    # along the top and bottom floor edges
    zs = np.concatenate([[z], z - height / 2 - out, z + height / 2 + out])
    sub = np.concatenate([[wall], dy, dy])
    depth = (body.half_width(xs[:, None], zs[None, :]) - sub[None, :]).min()
    # around the end floor edges (the body tapers in x too)
    xe = np.concatenate([x - length / 2 - out, x + length / 2 + out])
    ze = np.array([z - height / 2, z, z + height / 2])
    end = body.half_width(xe[:, None], ze[None, :]) - np.concatenate([dy, dy])[:, None]
    return float(min(depth, end.min()))


def size_glue_face_pocket(
    table: StationTable,
    shape: str,
    x: float,
    z_target: float,
    volume_mm3: float,
    wall: float,
    corner_r: float,
    x_limits: tuple[float, float],
    max_length: float = 30.0,
    min_depth: float = 0.8,
    kind: str = "ballast",
) -> GlueFacePocket:
    """Size a glue-face pocket centred at x that holds `volume_mm3` (both halves).

    Searches pocket height and centre height (from `z_target` upward, since a low ballast
    is preferred) and keeps the lowest centre that fits with `wall` of wood all round and a
    length ≤ `max_length`; ties go to the shortest pocket.
    """
    body = _Body(table, shape)
    cz, v = (float(a) for a in body.at(x))
    h_min = 2 * corner_r + 0.2
    best: tuple[float, float, GlueFacePocket] | None = None
    for z in np.arange(z_target, cz + 1e-9, 0.5):
        for height in np.arange(h_min, 2 * (v - wall) + 1e-9, 0.5):
            length = 2 * corner_r + 0.2
            pocket = None
            for _ in range(8):  # depth depends on the length and vice versa
                depth = _max_depth(body, x, length, z, height, wall)
                if depth < min_depth:
                    break
                need = (volume_mm3 / (2 * depth) + (4 - math.pi) * corner_r**2) / height
                length = max(need, 2 * corner_r + 0.2)
                trial = GlueFacePocket(x, float(z), length, float(height), depth, corner_r, kind)
                if abs(trial.volume - volume_mm3) <= 1e-3 * volume_mm3 and _fits(
                    body, trial, wall, *x_limits
                ):
                    pocket = trial
                    break
            if pocket is None or pocket.length > max_length:
                continue
            key = (round(float(z), 3), pocket.length)
            if best is None or key < best[:2]:
                best = (*key, pocket)
        if best is not None:
            break
    if best is None:
        raise CncGeometryError(
            f"no room for a {volume_mm3 / 1000:.3f} cm³ glue-face {kind} pocket at x = {x:.1f} "
            f"(wall {wall} mm, max length {max_length} mm, min depth {min_depth} mm)"
        )
    return best[2]


def pocket_corner_r(cfg) -> float:
    """Pocket corner radius: the pocket tool's radius plus `cnc.corner_clearance_mm`."""
    return tool_diameter_from_id(cfg.cnc.ops.pocket.tool) / 2 + cfg.cnc.corner_clearance_mm


def _section_x_limits(table: StationTable, spec, section: str, wall: float, depth: float):
    """x range a pocket of `depth` may span in a section: clear of the tips, the head's V
    faces (which lean `tan(angle)` per mm of y forward of the apex) and the tail's pin bore."""
    lean = depth * math.tan(math.radians(spec.angle_deg))
    if section == "head":
        return table.nose[0] + wall, spec.head_apex_x - wall - lean
    return spec.pin_x + spec.pin_bore_d / 2 + wall, table.tail[0] - wall


def ballast_pocket(cfg, table: StationTable, shape: str, spec, x: float, volume_cm3: float):
    """The head's glue-face ballast pocket for `volume_cm3` of ballast at x."""
    wall = cfg.dfm.cnc.min_wood_wall_mm
    body = _Body(table, shape)
    cz, v = (float(a) for a in body.at(x))
    hw_max = float(body.half_width(x, cz))
    limits = _section_x_limits(table, spec, "head", wall, hw_max)
    return size_glue_face_pocket(
        table,
        shape,
        x,
        cz - v + cfg.ballast.z_above_belly_mm,
        1000.0 * volume_cm3,
        wall,
        pocket_corner_r(cfg),
        limits,
        cfg.cnc.ballast_max_length_mm,
        cfg.cnc.ballast_min_depth_mm,
    )


def air_pockets(cfg, table: StationTable, shape: str, spec) -> list[GlueFacePocket]:
    """`cnc.air_pockets` as pockets, each checked for walls (kind `air:<section>`)."""
    wall = cfg.dfm.cnc.min_wood_wall_mm
    body = _Body(table, shape)
    out = []
    for a in cfg.cnc.air_pockets:
        p = GlueFacePocket(
            a.x_mm,
            a.z_mm,
            a.length_mm,
            a.height_mm,
            a.depth_mm,
            pocket_corner_r(cfg),
            f"air:{a.section}",
        )
        if 2 * p.corner_r > min(p.length, p.height):
            raise CncGeometryError(f"air pocket at x = {a.x_mm} is narrower than the tool")
        limits = _section_x_limits(table, spec, a.section, wall, a.depth_mm)
        if not _fits(body, p, wall, *limits):
            raise CncGeometryError(
                f"air pocket at x = {a.x_mm}, z = {a.z_mm} breaks the {wall} mm wall or "
                f"leaves the {a.section}"
            )
        out.append(p)
    return out


# --- section meshes → halves and side-view outlines --------------------------------------


def split_halves(mesh):
    """(right, left) halves of a section mesh: y ≥ 0 and y ≤ 0, closed at the glue face."""
    import manifold3d as mf
    import trimesh

    m = mf.Manifold(
        mf.Mesh(
            vert_properties=np.asarray(mesh.vertices, np.float32),
            tri_verts=np.asarray(mesh.faces, np.uint32),
        )
    )
    if m.status() != mf.Error.NoError:
        raise CncGeometryError(f"section mesh is not a closed manifold: {m.status()}")
    right, left = m.split_by_plane((0.0, 1.0, 0.0), 0.0)
    halves = []
    for h in (right, left):
        out = h.to_mesh()
        halves.append(
            trimesh.Trimesh(
                np.asarray(out.vert_properties)[:, :3], np.asarray(out.tri_verts), process=True
            )
        )
    return halves[0], halves[1]


def silhouette_xz(mesh) -> Polygon:
    """Side-view silhouette (projection along y) as one polygon in (x, z)."""
    v = np.asarray(mesh.vertices)[:, [0, 2]]
    tris = shapely.polygons(v[np.asarray(mesh.faces)])
    tris = tris[shapely.area(tris) > 1e-9]
    out = shapely.union_all(tris).buffer(0)
    if out.geom_type == "MultiPolygon":
        out = max(out.geoms, key=lambda g: g.area)
    return Polygon(out.exterior)  # holes in a projection are slivers; the cut-out ignores them


def glue_face_xz(mesh, y: float = 1e-3) -> Polygon | shapely.MultiPolygon:
    """The section cut by the plane y = `y` (just off the glue face), in (x, z), with holes."""
    sec = mesh.section(plane_origin=[0, y, 0], plane_normal=[0, 1, 0])
    loops = [] if sec is None else sec.discrete
    rings = [Polygon(d[:, [0, 2]]) for d in loops if len(d) >= 4]
    if not rings:
        raise CncGeometryError("section does not cross the glue face")
    # Rings nest (outer, pocket, …): even-odd, so a ring inside another one is a hole.
    out = rings[0].buffer(0)
    for ring in rings[1:]:
        out = out.symmetric_difference(ring.buffer(0))
    return out


def max_half_width(mesh) -> float:
    return float(np.abs(np.asarray(mesh.vertices)[:, 1]).max())
