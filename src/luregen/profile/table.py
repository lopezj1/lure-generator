"""StationTable: the contract between profile extraction (F1) and envelope geometry (F2).

A lure body is described by its nose and tail tip points plus N interior stations. Each
station is a cross-section at `x` centered at `center_z`, with vertical semi-axis
`v_radius` and lateral semi-axis `half_width`. Coordinates follow the package convention
(see `luregen.__doc__`): x from the nose tip toward the tail, z up, mm.

CSV format (v1):

    # luregen-stations v1
    # units: mm
    # nose: 0.000, 0.000
    # tail: 175.000, 1.200
    # source: <free text>
    x,center_z,v_radius,half_width
    4.000,0.120,3.100,1.050
    ...
"""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FORMAT_TAG = "luregen-stations v1"
COLUMNS = ("x", "center_z", "v_radius", "half_width")


class StationTableError(ValueError):
    pass


@dataclass(frozen=True)
class StationTable:
    nose: tuple[float, float]  # (x, z) of the nose tip
    tail: tuple[float, float]  # (x, z) of the tail tip
    x: np.ndarray
    center_z: np.ndarray
    v_radius: np.ndarray
    half_width: np.ndarray
    source: str = ""
    meta: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in COLUMNS:
            arr = np.asarray(getattr(self, name), dtype=float).copy()
            arr.setflags(write=False)
            object.__setattr__(self, name, arr)
        object.__setattr__(self, "nose", (float(self.nose[0]), float(self.nose[1])))
        object.__setattr__(self, "tail", (float(self.tail[0]), float(self.tail[1])))
        self.validate()

    def validate(self) -> None:
        n = len(self.x)
        if n < 2:
            raise StationTableError("need at least 2 stations")
        if any(len(getattr(self, c)) != n for c in COLUMNS):
            raise StationTableError("all station columns must have the same length")
        if not all(np.isfinite(getattr(self, c)).all() for c in COLUMNS):
            raise StationTableError("station values must be finite")
        if np.any(np.diff(self.x) <= 0):
            raise StationTableError("station x must be strictly increasing")
        if not (self.nose[0] < self.x[0] and self.x[-1] < self.tail[0]):
            raise StationTableError("stations must lie strictly between the nose and tail tips")
        if np.any(self.v_radius <= 0) or np.any(self.half_width <= 0):
            raise StationTableError("v_radius and half_width must be > 0 (tips are the vertices)")

    def __len__(self) -> int:
        return len(self.x)

    @property
    def length(self) -> float:
        return self.tail[0] - self.nose[0]

    @property
    def max_depth(self) -> float:
        return float(2 * self.v_radius.max())

    @property
    def max_width(self) -> float:
        return float(2 * self.half_width.max())

    def scaled(self, factor: float) -> "StationTable":
        """Uniformly scale every dimension about the origin (used for weight → length)."""
        if factor <= 0:
            raise StationTableError("scale factor must be > 0")
        return StationTable(
            nose=(self.nose[0] * factor, self.nose[1] * factor),
            tail=(self.tail[0] * factor, self.tail[1] * factor),
            x=self.x * factor,
            center_z=self.center_z * factor,
            v_radius=self.v_radius * factor,
            half_width=self.half_width * factor,
            source=self.source,
            meta={**self.meta, "scaled_by": f"{factor:.6g}"},
        )

    def to_csv(self, path: Path) -> None:
        lines = [
            f"# {FORMAT_TAG}",
            "# units: mm",
            f"# nose: {self.nose[0]:.3f}, {self.nose[1]:.3f}",
            f"# tail: {self.tail[0]:.3f}, {self.tail[1]:.3f}",
            f"# source: {self.source}",
            *(f"# {k}: {v}" for k, v in self.meta.items()),
            ",".join(COLUMNS),
        ]
        rows = np.column_stack([getattr(self, c) for c in COLUMNS])
        lines += [",".join(f"{v:.3f}" for v in row) for row in rows]
        path.write_text("\n".join(lines) + "\n")

    @classmethod
    def load_csv(cls, path: Path) -> "StationTable":
        header: dict[str, str] = {}
        data_lines: list[str] = []
        for line in path.read_text().splitlines():
            if line.startswith("#"):
                key, _, value = line[1:].strip().partition(":")
                header[key.strip()] = value.strip()
            elif line.strip():
                data_lines.append(line)
        if FORMAT_TAG not in header:
            raise StationTableError(f"{path}: missing '# {FORMAT_TAG}' header")
        if header.get("units", "mm") != "mm":
            raise StationTableError(f"{path}: units must be mm")
        if not data_lines or tuple(c.strip() for c in data_lines[0].split(",")) != COLUMNS:
            raise StationTableError(f"{path}: column header must be {','.join(COLUMNS)}")
        rows = np.array([[float(v) for v in ln.split(",")] for ln in data_lines[1:]], ndmin=2)
        try:
            nose = tuple(float(v) for v in header.pop("nose").split(","))
            tail = tuple(float(v) for v in header.pop("tail").split(","))
        except KeyError as exc:
            raise StationTableError(f"{path}: missing '# {exc.args[0]}: x, z' header") from exc
        source = header.pop("source", "")
        for key in (FORMAT_TAG, "units"):
            header.pop(key, None)
        return cls(nose, tail, *rows.T, source=source, meta=header)
