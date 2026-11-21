"""Schemas for `spec/routers/<machine>/{post,machine,tools}.toml`.

`post.toml` describes the G-code dialect (header/footer templates, precision, arcs,
drilling, comments, forbidden codes). Templates are G-code lines with `{token}` fields;
after substitution each line is re-tokenized and re-emitted with the configured word
separator and decimals, so `"G53 G0 Z-5"` may come out as `G53G0Z-5.000`.
"""

import string
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from luregen.fab.router.ir import Tool

# Tokens a template may use, by template group.
JOB_TOKENS = {"design", "setup", "zero", "stock_min", "stock_max", "stock_block", "safe_z"}
TOOL_TOKENS = JOB_TOKENS | {"tool_number", "tool_id", "tool_name", "dia", "corner_r", "op", "rpm"}
SPINDLE_TOKENS = TOOL_TOKENS | {"dial"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def template_fields(line: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(line) if name}


def _check_tokens(lines: list[str], allowed: set[str], where: str) -> None:
    for line in lines:
        unknown = template_fields(line) - allowed
        if unknown:
            raise ValueError(f"{where}: unknown token(s) {sorted(unknown)} in {line!r}")


class Decimals(Strict):
    xyz: int = Field(3, ge=0, le=6)
    ijk: int = Field(3, ge=0, le=6)
    feed: int = Field(1, ge=0, le=4)
    rpm: int = Field(0, ge=0, le=2)
    dwell: int = Field(2, ge=0, le=3)
    trim_zeros: bool = False  # True: "X50.8 Y0" instead of "X50.800Y0.000"

    def for_letter(self, letter: str) -> int:
        return {
            "X": self.xyz,
            "Y": self.xyz,
            "Z": self.xyz,
            "I": self.ijk,
            "J": self.ijk,
            "K": self.ijk,
            "R": self.ijk,
            "F": self.feed,
            "S": self.rpm,
            "P": self.dwell,
        }.get(letter, 0)


class Modal(Strict):
    motion: bool = True  # omit G0/G1/G2/G3 when unchanged
    axes: bool = True  # omit axis words whose formatted value is unchanged
    feed: bool = True  # omit F when unchanged


class Arcs(Strict):
    enabled: bool = True
    format: Literal["ij"] = "ij"  # incremental IJK centers; R format is not emitted
    planes: list[Literal["xy", "zx", "yz"]] = ["xy", "zx", "yz"]  # others are linearized
    max_sweep_deg: float = Field(180.0, gt=0, le=180)
    min_radius: float = Field(0.1, ge=0)
    min_chord: float = Field(0.25, ge=0)
    snap_endpoint: bool = True
    linearize_tol: float = Field(0.01, gt=0, description="Max sagitta when an arc is split.")


class SpindleCfg(Strict):
    type: Literal["router_dial", "vfd"] = "router_dial"
    on: list[str] = ["M3 S{rpm}"]
    dwell_s: float = Field(3.0, ge=0)


class DrillCfg(Strict):
    mode: Literal["expand"] = "expand"
    clearance: float = Field(0.5, ge=0, description="Rapid back to this far above the last peck.")


class CommentCfg(Strict):
    max_length: int = Field(80, ge=10, description="Whole comment line, parentheses included.")
    allowed: str = Field(" .,:_/+=#*%-", description="Allowed besides letters and digits.")
    replacement: str = "_"


class OutputCfg(Strict):
    """How a setup's program is written out (#43)."""

    split_by_tool: bool = Field(
        True,
        description="One file per tool (<setup>_<nn>_<tool>.nc), each with its own M6 block; "
        "false: one file per setup with an M6 block at each tool change.",
    )


class PostConfig(Strict):
    name: str = "GRBL"
    units: Literal["mm"] = "mm"
    word_separator: Literal["", " "] = ""
    max_line_length: int = Field(80, ge=20)
    decimals: Decimals = Decimals()
    modal: Modal = Modal()
    header: list[str] = ["G90", "G21", "G17"]
    footer: list[str] = ["M5", "M30"]
    safe_retract: str = "G53 G0 Z-5"
    tool_change: list[str] = ["M5", "G53 G0 Z-5", "M6 T{tool_number}"]
    spindle: SpindleCfg = SpindleCfg()
    arcs: Arcs = Arcs()
    drill: DrillCfg = DrillCfg()
    comments: CommentCfg = CommentCfg()
    output: OutputCfg = OutputCfg()
    forbid: list[str] = ["G10", "G92", "G41", "G42", "G43", "G80", "G81", "G82", "G83"]

    @model_validator(mode="after")
    def tokens_known(self) -> "PostConfig":
        _check_tokens(self.header, JOB_TOKENS, "header")
        _check_tokens(self.footer, JOB_TOKENS, "footer")
        _check_tokens([self.safe_retract], JOB_TOKENS, "safe_retract")
        _check_tokens(self.tool_change, TOOL_TOKENS, "tool_change")
        _check_tokens(self.spindle.on, SPINDLE_TOKENS, "spindle.on")
        return self


class Travel(Strict):
    x: float = Field(gt=0)
    y: float = Field(gt=0)
    z: float = Field(gt=0)
    verified: bool = False


class Controller(Strict):
    kind: str
    firmware: str = ""
    # Codes the controller accepts; anything else is an error in `verify`.
    supported: list[str] = []


class RouterSpindle(Strict):
    kind: str = "unknown"
    min_rpm: float = 0
    max_rpm: float = 0
    # rpm at dial settings 1, 2, …; empty when the router has no dial or it's unknown.
    dial_rpm: list[float] = []
    verified: bool = False


class Workholding(Strict):
    spoilboard_mm: float | None = Field(None, gt=0, description="Spoilboard thickness.")
    work_area_mm: tuple[float, float] | None = Field(None, description="Largest blank, X × Y.")
    clamps: str = Field("", description="How the blank is held (setup sheet text).")
    min_margin_mm: float = Field(0.0, ge=0, description="Waste border the clamps need.")


class MachineConfig(Strict):
    name: str
    controller: Controller
    travel: Travel
    homing_corner: str = "unknown"
    safe_z_machine: float = Field(-5.0, le=0, description="G53 Z used for retracts.")
    rapid_mm_min: tuple[float, float, float] = (5000.0, 5000.0, 2500.0)
    rapid_verified: bool = False
    accel_mm_s2: tuple[float, float, float] | None = None
    workholding: Workholding = Workholding()
    tool_change_s: float = Field(60.0, ge=0, description="Runtime estimate per M6.")
    bitsetter: bool | None = Field(None, description="Tool-length probe present; None = unknown.")
    spindle: RouterSpindle = RouterSpindle()


class ToolEntry(Strict):
    number: int = Field(ge=0)
    shape: Literal["flat", "ball"]
    diameter: float = Field(gt=0)
    corner_radius: float | None = None
    flutes: int | None = None
    name: str = ""
    number_verified: bool = False
    flute_length: float | None = Field(None, gt=0, description="mm; 3D ops check reach.")
    shank_diameter: float | None = Field(None, gt=0, description="mm; default = diameter.")
    taper_deg: float | None = Field(
        None, gt=0, lt=45, description="Tapered ball nose: cone half-angle; diameter = tip."
    )

    def to_tool(self, tool_id: str) -> Tool:
        return Tool(
            id=tool_id,
            number=self.number,
            shape=self.shape,
            diameter=self.diameter,
            corner_radius=self.corner_radius,
            name=self.name,
        )


class ToolLibrary(Strict):
    tools: dict[str, ToolEntry]

    def get(self, tool_id: str) -> Tool:
        if tool_id not in self.tools:
            raise KeyError(f"tool {tool_id!r} not in library ({', '.join(self.tools)})")
        return self.tools[tool_id].to_tool(tool_id)


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def load_post(path: Path) -> PostConfig:
    return PostConfig.model_validate(_load(path))


def load_machine(path: Path) -> MachineConfig:
    return MachineConfig.model_validate(_load(path))


def load_tools(path: Path) -> ToolLibrary:
    return ToolLibrary.model_validate(_load(path))


def router_dir(spec: Path, machine: str) -> Path:
    path = spec / "routers" / machine
    if not path.is_dir():
        raise FileNotFoundError(f"no router machine folder: {path}")
    return path
