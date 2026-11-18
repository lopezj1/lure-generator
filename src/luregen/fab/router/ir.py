"""Machine-neutral toolpath IR: what CAM produces and a post-processor consumes.

A `Program` is one setup (one G-code file): stock and zero metadata, a tool table and an
ordered list of `Operation`s. Each operation names its tool and spindle speed; the post
inserts the tool change and spindle start whenever they differ from the previous operation.
Moves use absolute work coordinates in mm; `None` on a linear move means "axis unchanged".
Drilling stays a canned `Drill` move here and the post expands it for controllers without
canned cycles.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Plane = Literal["xy", "zx", "yz"]
# Which coordinate names a plane needs, for an arc's end point and center.
_PLANE_COORDS: dict[str, tuple[str, str]] = {"xy": ("x", "y"), "zx": ("z", "x"), "yz": ("y", "z")}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rapid(Strict):
    kind: Literal["rapid"] = "rapid"
    x: float | None = None
    y: float | None = None
    z: float | None = None


class Feed(Strict):
    kind: Literal["feed"] = "feed"
    x: float | None = None
    y: float | None = None
    z: float | None = None
    f: float = Field(gt=0, description="Feed rate, mm/min.")


class Arc(Strict):
    """Circular (or helical) move from the current point to the end, around `c*`."""

    kind: Literal["arc"] = "arc"
    x: float | None = None
    y: float | None = None
    z: float | None = None
    cx: float | None = None
    cy: float | None = None
    cz: float | None = None
    plane: Plane = "xy"
    cw: bool = True
    f: float = Field(gt=0)

    @model_validator(mode="after")
    def in_plane_coords(self) -> "Arc":
        for name in _PLANE_COORDS[self.plane]:
            if getattr(self, name) is None or getattr(self, "c" + name) is None:
                raise ValueError(f"{self.plane} arc needs {name} and c{name}")
        return self

    def plane_coords(self) -> tuple[str, str]:
        return _PLANE_COORDS[self.plane]


class Drill(Strict):
    """Peck drill at (x, y): rapid to `r_z`, peck down to `bottom_z`, end at `r_z`."""

    kind: Literal["drill"] = "drill"
    x: float
    y: float
    r_z: float = Field(description="Retract plane, above the stock.")
    bottom_z: float
    peck: float | None = Field(None, gt=0, description="Peck depth; None drills in one pass.")
    f: float = Field(gt=0)
    dwell_s: float = Field(0.0, ge=0)

    @model_validator(mode="after")
    def bottom_below_r(self) -> "Drill":
        if self.bottom_z >= self.r_z:
            raise ValueError("drill bottom_z must be below r_z")
        return self


class Dwell(Strict):
    kind: Literal["dwell"] = "dwell"
    seconds: float = Field(gt=0)


class Comment(Strict):
    kind: Literal["comment"] = "comment"
    text: str


class Pause(Strict):
    """Program stop the operator resumes (M0), e.g. to check tabs or clamps."""

    kind: Literal["pause"] = "pause"
    message: str = ""


Move = Annotated[Rapid | Feed | Arc | Drill | Dwell | Comment | Pause, Field(discriminator="kind")]


class Tool(Strict):
    id: str
    number: int = Field(ge=0, description="Controller tool number (M6 T<n>).")
    shape: Literal["flat", "ball"]
    diameter: float = Field(gt=0)
    corner_radius: float | None = Field(None, ge=0, description="Defaults: flat 0, ball d/2.")
    name: str = ""

    @property
    def radius(self) -> float:
        return self.diameter / 2

    @property
    def corner(self) -> float:
        if self.corner_radius is not None:
            return self.corner_radius
        return self.radius if self.shape == "ball" else 0.0


class Operation(Strict):
    name: str
    tool: str = Field(description="Key into Program.tools.")
    rpm: float = Field(gt=0)
    moves: list[Move] = []


class Setup(Strict):
    """Stock box in work coordinates and a human description of where zero is."""

    name: str = "setup1"
    stock_min: tuple[float, float, float]
    stock_max: tuple[float, float, float]
    zero: str = "front-left corner, stock top"
    safe_z: float = Field(5.0, description="Work Z at which rapids clear stock and clamps.")

    @model_validator(mode="after")
    def ordered(self) -> "Setup":
        if any(lo >= hi for lo, hi in zip(self.stock_min, self.stock_max, strict=True)):
            raise ValueError("stock_min must be below stock_max on every axis")
        if self.safe_z <= self.stock_max[2]:
            raise ValueError("safe_z must be above the stock top")
        return self


class Program(Strict):
    design: str
    setup: Setup
    tools: dict[str, Tool]
    operations: list[Operation] = []

    @model_validator(mode="after")
    def tools_exist(self) -> "Program":
        for key, tool in self.tools.items():
            if tool.id != key:
                raise ValueError(f"tool key {key!r} != tool.id {tool.id!r}")
        for op in self.operations:
            if op.tool not in self.tools:
                raise ValueError(f"operation {op.name!r} uses unknown tool {op.tool!r}")
        return self

    def to_json(self) -> str:
        return self.model_dump_json(indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Program":
        return cls.model_validate_json(text)

    def save(self, path: Path) -> None:
        path.write_text(self.to_json() + "\n")

    @classmethod
    def load(cls, path: Path) -> "Program":
        return cls.from_json(path.read_text())
