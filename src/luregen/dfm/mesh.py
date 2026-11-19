"""Mesh queries for the DFM rules, in numpy/scipy only (trimesh's proximity and ray
queries need rtree/embree, which the project does not depend on)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import trimesh

CANDIDATES = 32  # nearest triangle centroids checked exactly per query point


@dataclass
class Shells:
    """A section mesh split into its outer shell and inner shells (sealed voids).

    Every shell's normals point out of its own enclosed volume: the outer shell's out of
    the part, an inner shell's out of the void, i.e. into the material around it.
    """

    outer: trimesh.Trimesh
    inners: list[trimesh.Trimesh]


def split_shells(mesh: trimesh.Trimesh) -> Shells:
    parts = mesh.split(only_watertight=False)
    if not len(parts):
        raise ValueError("empty mesh")
    parts = sorted(parts, key=lambda p: abs(p.volume), reverse=True)
    oriented = []
    for p in parts:
        p.fix_normals()
        # rebuild: STL-loaded face normals can stay cached through the winding fix
        oriented.append(trimesh.Trimesh(p.vertices, p.faces, process=False))
    return Shells(oriented[0], oriented[1:])


def closest_points(mesh: trimesh.Trimesh, points) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(closest points, distances, triangle ids) on `mesh` for each query point."""
    from scipy.spatial import cKDTree

    pts = np.atleast_2d(np.asarray(points, dtype=float))
    tris = mesh.triangles
    k = min(CANDIDATES, len(tris))
    _, cand = cKDTree(mesh.triangles_center).query(pts, k=k)
    cand = np.asarray(cand).reshape(len(pts), k)
    flat_pts = np.repeat(pts, k, axis=0)
    close = trimesh.triangles.closest_point(tris[cand.ravel()], flat_pts)
    dist = np.linalg.norm(close - flat_pts, axis=1).reshape(len(pts), k)
    best = dist.argmin(axis=1)
    rows = np.arange(len(pts))
    return close.reshape(len(pts), k, 3)[rows, best], dist[rows, best], cand[rows, best]


def min_distance(a: trimesh.Trimesh, b: trimesh.Trimesh) -> tuple[float, np.ndarray]:
    """Smallest distance from a's vertices to surface b, and the vertex where it occurs."""
    _, dist, _ = closest_points(b, a.vertices)
    i = int(dist.argmin())
    return float(dist[i]), a.vertices[i]


def ray_hits(mesh: trimesh.Trimesh, origin, direction) -> np.ndarray:
    """Sorted distances along a ray to every triangle it crosses (Möller–Trumbore)."""
    o = np.asarray(origin, dtype=float)
    d = np.asarray(direction, dtype=float)
    d = d / np.linalg.norm(d)
    v0, v1, v2 = (mesh.triangles[:, i] for i in range(3))
    e1, e2 = v1 - v0, v2 - v0
    p = np.cross(d, e2)
    det = np.einsum("ij,ij->i", e1, p)
    ok = np.abs(det) > 1e-12
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    s = o - v0
    u = np.einsum("ij,ij->i", s, p) * inv
    q = np.cross(s, e1)
    v = (q @ d) * inv
    t = np.einsum("ij,ij->i", e2, q) * inv
    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9)
    return np.sort(t[hit])


def first_hit(mesh: trimesh.Trimesh, origin, direction) -> float:
    hits = ray_hits(mesh, origin, direction)
    return float(hits[0]) if len(hits) else float("inf")


def face_regions(mesh: trimesh.Trimesh, faces: np.ndarray) -> list[np.ndarray]:
    """Edge-connected groups among the selected face ids."""
    if not len(faces):
        return []
    adj = mesh.face_adjacency
    mask = np.isin(adj, faces).all(axis=1)
    return [np.asarray(c) for c in trimesh.graph.connected_components(adj[mask], nodes=faces)]
