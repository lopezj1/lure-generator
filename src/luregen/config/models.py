"""Pydantic models for a lure build config.

A variant TOML (after `extends` merging and `--set` overrides) validates into `LureConfig`.
`resolve()` then fills catalog-derived fields (hardware masses, densities), so a resolved
config is self-contained: `resolved.toml` reloads without the spec catalogs.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Range = tuple[float, float]


class SizeConfig(Strict):
    length_mm: float | None = Field(None, ge=100, le=250)
    target_weight_oz: float | None = Field(None, gt=0)

    @model_validator(mode="after")
    def exactly_one(self) -> "SizeConfig":
        if (self.length_mm is None) == (self.target_weight_oz is None):
            raise ValueError("set exactly one of size.length_mm and size.target_weight_oz")
        return self


class WidthRule(Strict):
    mode: Literal["ratio_of_depth"] = "ratio_of_depth"
    value: float = Field(0.34, gt=0, le=1.5)


class Smoothing(Strict):
    method: Literal["efd", "spline"] = "efd"
    harmonics: int = Field(20, ge=4, le=60)
    spline_s: float | None = None


class ProfileConfig(Strict):
    source: Literal["image", "preset"]
    image: Path | None = None
    top_image: Path | None = None
    preset: Path | None = None
    scale: Literal["aruco", "length"] = "length"
    aruco_marker_mm: float = Field(30.0, gt=0)
    n_stations: int = Field(23, ge=8, le=30)
    section_shape: Literal["ellipse", "egg", "teardrop"] = "ellipse"
    width_rule: WidthRule = WidthRule()
    smoothing: Smoothing = Smoothing()

    @model_validator(mode="after")
    def source_file(self) -> "ProfileConfig":
        if self.source == "image" and self.image is None:
            raise ValueError("profile.source = 'image' requires profile.image")
        if self.source == "preset" and self.preset is None:
            raise ValueError("profile.source = 'preset' requires profile.preset")
        return self


class JointConfig(Strict):
    joint_frac: float = Field(0.50, ge=0.45, le=0.55)
    gap_mm: float = Field(2.3, ge=1.5, le=3.5)
    angle_deg: float = Field(37.0, ge=30, le=45)
    hinge_eye_z: tuple[float, float] = (-12.0, 6.0)
    line_tie_z: float = Field(-4.0, ge=-6, le=0)


class BuoyancyConfig(Strict):
    water: Literal["salt", "fresh"] = "salt"
    water_density: float | None = None  # filled by resolve()
    target_sink: Literal["float", "suspend", "slow_sink"] = "slow_sink"
    sink_bands: dict[Literal["float", "suspend", "slow_sink"], Range]
    head_density: Range = (1.00, 1.05)
    tail_density: Range = (0.70, 0.80)
    head_tail_mass_ratio: float = Field(2.4, gt=0)
    level_tolerance_mm: float = Field(1.0, gt=0)
    cb_above_cg_frac: float = Field(0.08, ge=0)
    trim_pocket: bool = True


class BallastConfig(Strict):
    material: str = "tungsten"
    density: float | None = None  # filled by resolve()
    mass_g: float | None = Field(None, gt=0, description="Pin the ballast mass; else solved.")
    x_from_joint_mm: float = 22.0
    z_above_belly_mm: float = 6.0


class HardwareItem(Strict):
    """A hardware point mass. `id` refers to spec/hardware; unset fields come from the catalog."""

    id: str
    section: Literal["head", "tail"]
    x_mm: float
    z_mm: float
    qty: int = Field(1, ge=1)
    name: str | None = None
    mass_g: Range | None = None
    material: str | None = None
    density: float | None = None
    measured: bool | None = None


class ProcessConfig(Strict):
    method: Literal["print", "cnc"]
    body_style: Literal["loft3d", "profile25d"] = "loft3d"
    material: str
    material_density: float | None = Field(
        None, gt=0, description="Override, e.g. a weighed blank."
    )
    rigging: Literal["screw_eye", "through_wire"] = "screw_eye"


class PrintDfm(Strict):
    min_wall_mm: float = 1.6
    cavity_roof: Literal["gable45", "teardrop"] = "gable45"
    min_skin_to_cavity_mm: float = 1.2


class CncDfm(Strict):
    tools: list[str] = ["flat_6.35", "flat_3.175", "ball_3.175"]
    min_wood_wall_mm: float = 3.5
    max_pocket_depth_x_dia: float = 3.0
    roundover_r_mm: float = 4.0


class DfmConfig(Strict):
    print: PrintDfm = PrintDfm()
    cnc: CncDfm = CncDfm()


class ReportConfig(Strict):
    show_oz: bool = True


class LureConfig(Strict):
    name: str
    family: str
    size: SizeConfig
    profile: ProfileConfig
    joint: JointConfig | None = None
    buoyancy: BuoyancyConfig
    ballast: BallastConfig = BallastConfig()
    hardware: list[HardwareItem] = []
    process: ProcessConfig
    dfm: DfmConfig = DfmConfig()
    report: ReportConfig = ReportConfig()

    @property
    def is_resolved(self) -> bool:
        return (
            self.buoyancy.water_density is not None
            and self.ballast.density is not None
            and self.process.material_density is not None
            and all(h.mass_g is not None and h.density is not None for h in self.hardware)
        )
