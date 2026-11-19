"""What the DFM rules look at: the build's report geometry plus its section meshes.

`lure build` and `lure check` build the same `DfmModel` (from the in-memory report blocks
or from report.json, and from model/<part>.stl), so a build folder can be re-checked
without rebuilding the CAD model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import trimesh

from luregen.dfm.mesh import Shells, split_shells

SECTIONS = ("head", "tail")


@dataclass
class DfmModel:
    geometry: dict[str, Any]  # report["geometry"] (family block: joint, pockets, internals)
    envelope: dict[str, Any] = field(default_factory=dict)  # report["envelope"]
    meshes: dict[str, trimesh.Trimesh] = field(default_factory=dict)  # head, tail, body
    _shells: dict[str, Shells] = field(default_factory=dict, repr=False)

    @property
    def sections(self) -> list[str]:
        return [s for s in SECTIONS if s in self.meshes]

    def shells(self, part: str) -> Shells:
        if part not in self._shells:
            self._shells[part] = split_shells(self.meshes[part])
        return self._shells[part]

    @classmethod
    def load(cls, model_dir: Path, geometry: dict, envelope: dict | None = None) -> DfmModel:
        meshes = {}
        for part in (*SECTIONS, "body"):
            path = model_dir / f"{part}.stl"
            if path.is_file():
                meshes[part] = trimesh.load(path, force="mesh")
        return cls(geometry or {}, envelope or {}, meshes)

    @classmethod
    def from_build_dir(cls, build_dir: Path) -> DfmModel:
        report = json.loads((build_dir / "report.json").read_text())
        if not report.get("geometry"):
            raise FileNotFoundError(f"{build_dir}: report.json has no geometry block (not built)")
        return cls.load(build_dir / "model", report["geometry"], report.get("envelope"))
