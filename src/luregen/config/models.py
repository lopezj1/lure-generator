"""Pydantic models for a lure build config.

A variant TOML (after `extends` merging and `--set` overrides) validates into `LureConfig`.
`resolve()` then fills catalog-derived fields (hardware masses, densities), so a resolved
config is self-contained: `resolved.toml` reloads without the spec catalogs.
"""

import warnings
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Range = tuple[float, float]


class SizeConfig(Strict):
    length_mm: float | None = Field(None, ge=100, le=250)
    target_weight_oz: float | None = Field(None, gt=0)
    solved_length_mm: float | None = Field(
        None,
        ge=100,
        le=250,
        description="Length solved from target_weight_oz (written by `lure build`).",
    )

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
    angle_deg: float = Field(
        37.0, ge=30, le=45, description="V arm angle from the lateral (y) axis."
    )
    hinge_eye_z: tuple[float, float] = (-12.0, 6.0)
    line_tie_z: float = Field(-4.0, ge=-6, le=0)
    pin_offset_mm: float = Field(
        3.0, ge=1, le=10, description="Hinge pin axis behind the tail's V apex."
    )
    pin_bore_mm: float = Field(1.8, gt=0, le=4, description="Hinge pin bore diameter.")
    eye_slot_w_mm: float = Field(
        2.5, gt=0, le=6, description="Width (y) of the tail slots for the head hinge eyes."
    )
    eye_slot_h_mm: float = Field(
        6.0, gt=0, le=12, description="Height (z) of the tail slots for the head hinge eyes."
    )


class BuoyancyConfig(Strict):
    water: Literal["salt", "fresh"] = "salt"
    water_density: float | None = None  # filled by resolve()
    target_sink: Literal["float", "suspend", "slow_sink"] = "slow_sink"
    sink_bands: dict[Literal["float", "suspend", "slow_sink"], Range]
    # The head density is derived (sink band, tail density and head:tail ratio fix it), not
    # a target (#96). Configs and resolved.toml files from before that still load.
    tail_density: Range = (0.70, 0.80)
    head_tail_mass_ratio: float = Field(2.4, gt=0)
    level_tolerance_mm: float = Field(1.0, gt=0)
    cb_above_cg_frac: float = Field(0.08, ge=0)
    trim_pocket: bool = True

    @model_validator(mode="before")
    @classmethod
    def _drop_head_density(cls, data: Any) -> Any:
        if isinstance(data, dict) and "head_density" in data:
            warnings.warn(
                "buoyancy.head_density is no longer a target (the head density is derived); "
                "the key is ignored",
                DeprecationWarning,
                stacklevel=2,
            )
            data = {k: v for k, v in data.items() if k != "head_density"}
        return data


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


class SectionInternals(Strict):
    """Per-section print internals; these are the F4 solver knobs."""

    wall_thk_mm: float = Field(
        2.0, ge=0.8, le=10, description="Cavity wall: inset of v_radius and half_width."
    )
    keel_top_z_mm: float = Field(
        -8.0, description="Body z below which the section is solid (the keel)."
    )


class InternalsConfig(Strict):
    """Print internals: sealed cavities, ballast/trim pockets and screw-eye bosses."""

    head: SectionInternals = SectionInternals()
    tail: SectionInternals = SectionInternals(keel_top_z_mm=-12.0)
    feature_clearance_mm: float = Field(
        1.0, ge=0, description="Minimum solid between the cavity and any other feature."
    )
    boss_r_mm: float = Field(
        6.0, gt=0, description="Solid sphere kept around each screw-eye position."
    )
    ballast_seed_g: float = Field(
        8.0, gt=0, description="Ballast mass when ballast.mass_g is unset (F4 solves it)."
    )
    trim_x_from_tail_mm: float = Field(
        35.0, gt=0, description="Tail trim pocket position, measured from the tail tip."
    )
    trim_capacity_g: float | None = Field(
        None, gt=0, description="Trim putty capacity; default: hardware mass band + 1 g."
    )
    trim_material: str = "tungsten_putty"
    trim_fill_g: float | None = Field(
        None,
        ge=0,
        description="Nominal (hardware band mid) putty fill; the solver sets half the capacity.",
    )
    trim_density: float | None = None  # filled by resolve()


class SolverConfig(Strict):
    """Balance solver (F4). `lure build` writes `solved = true` and the solved knob values
    into resolved.toml, so rebuilding it reproduces the lure without solving again; pass
    `--set solver.solved=false` to solve again."""

    enabled: bool = True
    max_passes: int = Field(5, ge=1, le=10)
    solved: bool = Field(False, description="Knobs hold solved values; build without solving.")
    solved_knobs: list[str] = Field(
        [], description="Knobs the solver set (they are not user pins on a re-solve)."
    )
    weights: dict[str, float] = Field(
        {}, description="Least-squares weight overrides by target id (e.g. tail_density)."
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_head_density_weight(cls, data: Any) -> Any:
        w = data.get("weights") if isinstance(data, dict) else None
        if isinstance(w, dict) and "head_density" in w:
            warnings.warn(
                "solver.weights.head_density is ignored: the head density is not a target",
                DeprecationWarning,
                stacklevel=2,
            )
            data = {**data, "weights": {k: v for k, v in w.items() if k != "head_density"}}
        return data


class ProcessConfig(Strict):
    method: Literal["print", "cnc"]
    body_style: Literal["loft3d", "profile25d"] = "loft3d"
    material: str
    material_density: float | None = Field(
        None, gt=0, description="Override, e.g. a weighed blank."
    )
    rigging: Literal["screw_eye", "through_wire"] = "screw_eye"


class PrintDfm(Strict):
    """Print DFM thresholds (F5); the part is printed upright, z up."""

    min_wall_mm: float = 1.6
    cavity_roof: Literal["gable45", "teardrop"] = "gable45"
    min_skin_to_cavity_mm: float = 1.2
    max_overhang_deg: float = Field(
        45.0, gt=0, lt=90, description="Steepest cavity ceiling, measured from vertical."
    )
    overhang_tol_deg: float = Field(1.0, ge=0, description="Mesh noise allowance on the angle.")
    max_bridge_mm: float = Field(
        8.0, ge=0, description="Widest flatter-than-allowed ceiling patch printed as a bridge."
    )
    layer_height_mm: float = Field(0.2, gt=0, description="Layer height for the boss rule.")
    boss_min_layers: int = Field(
        20, ge=1, description="Warn when solid along a vertical hook pull is fewer layers."
    )
    boss_pull_axis_deg: float = Field(
        45.0, ge=0, le=90, description="Screw eyes within this of z pull across layers."
    )
    screw_eye_shank_mm: float = Field(5.0, gt=0, description="Screw-eye thread length.")
    screw_eye_shank_d_mm: float = Field(1.6, gt=0, description="Screw-eye thread diameter.")
    screw_eye_min_solid_mm: float = Field(
        0.8, ge=0, description="Solid between the screw-eye thread and any void."
    )


class CncDfm(Strict):
    tools: list[str] = ["flat_6.35", "flat_3.175", "ball_3.175"]
    min_wood_wall_mm: float = 3.5
    max_pocket_depth_x_dia: float = 3.0
    roundover_r_mm: float = 4.0
    corner_margin_mm: float = Field(
        0.1, ge=0, description="Inside corner radius ≥ tool radius + this."
    )
    undercut_tol_deg: float = Field(
        1.0, ge=0, description="Faces this far past vertical still count as reachable."
    )
    mesh_tol_mm: float = Field(0.05, ge=0, description="Mesh allowance on distance rules.")
    undercut_min_area_mm2: float = Field(
        1.0, ge=0, description="Ignore undercut patches smaller than this (tip mesh noise)."
    )


class DfmConfig(Strict):
    print: PrintDfm = PrintDfm()
    cnc: CncDfm = CncDfm()


class CncStock(Strict):
    """Blank for the CNC halves; zero is the front-left corner of the stock top."""

    thickness_mm: float | None = Field(
        None,
        gt=0,
        description="Measured board thickness; default: face + half width + top, rounded up "
        "to the next thickness_step_mm.",
    )
    thickness_step_mm: float = Field(
        6.35, ge=0, description="Auto thickness rounds up to this increment (1/4in); 0 = exact."
    )
    top_allowance_mm: float = Field(
        1.0, ge=0, description="Auto thickness: stock left above the outer apex."
    )
    face_mm: float = Field(
        0.0, ge=0, description="Facing depth on the glue-face side (setup 1); 0 = no facing."
    )
    margin_mm: float = Field(20.0, gt=0, description="Waste around the parts.")
    spacing_mm: float = Field(10.0, gt=0, description="Waste between two halves.")


class CncRegistration(Strict):
    """Flip pins: through holes on the flip axis, in the end waste, into the spoilboard."""

    enabled: bool = True
    pin_d_mm: float = Field(6.35, gt=0, description="Flip-pin dowel diameter (1/4in).")
    into_spoilboard_mm: float = Field(3.0, ge=0)


class CncWire(Strict):
    """Through-wire half-groove (process.rigging = "through_wire"), cut with a ball nose."""

    diameter_mm: float = Field(1.04, gt=0, description="Wire diameter (0.041in = 1.04 mm).")
    clearance_mm: float = Field(0.15, ge=0, description="Extra depth per half.")
    z_mm: float | None = Field(None, description="Groove height; default: joint.line_tie_z.")
    exit_mm: float = Field(3.0, ge=0, description="Groove run-out past the outline.")


class CncTabs(Strict):
    count: int = Field(4, ge=0, le=8, description="Holding tabs per half on the cut-out.")
    width_mm: float = Field(5.0, gt=0)
    height_mm: float = Field(1.5, gt=0)


class CncOp(Strict):
    """Tool and feeds for one CAM operation: a calculator mode or a named preset."""

    tool: str = Field(description="Key in spec/routers/<machine>/tools.toml.")
    mode: Literal["conservative", "aggressive"] = "conservative"
    preset: str | None = Field(None, description="Named preset in feeds.toml.")


# One class per op so each default lives on the field itself (the web editor drops values
# equal to the defaults key by key, so nested defaults must not differ from the class's).
class PocketOp(CncOp):
    tool: str = "flat_3.175"
    mode: Literal["conservative", "aggressive"] = "aggressive"


class GrooveOp(CncOp):
    tool: str = "ball_3.175"


class DrillOp(CncOp):
    tool: str = "flat_3.175"


class ProfileOp(CncOp):
    tool: str = "flat_3.175"
    preset: str | None = "owner_contour_flat_3.175"


class FaceOp(CncOp):
    tool: str = "flat_3.175"
    mode: Literal["conservative", "aggressive"] = "aggressive"


class Rough3dOp(CncOp):
    """Setup-2 z-level roughing of the outer face (#42)."""

    tool: str = "flat_3.175"
    preset: str | None = "owner_rough_flat_3.175"
    enabled: bool = True
    allowance_mm: float = Field(0.5, ge=0, description="Stock left for finishing.")
    max_stair_mm: float = Field(
        0.25, gt=0, description="Extra rest levels so shallow areas keep ≤ this stair."
    )
    resolution_mm: float = Field(0.15, gt=0.01, le=1.0, description="Heightmap cell size.")
    boundary_mm: float = Field(
        1.0, ge=0, description="Tool centres run this far past silhouette + r + allowance."
    )


class Finish3dOp(CncOp):
    """Setup-2 parallel raster finishing of the outer face (#42). Default: the owner's
    1/32in flat at 0.076 mm stepover; a ball nose uses the scallop stepover √(8·R·h)."""

    tool: str = "flat_0.794"
    preset: str | None = "owner_finish_flat_0.794"
    enabled: bool = True
    stepover_mm: float | None = Field(
        None, gt=0, description="Override; default: the preset (flat) or the scallop (ball)."
    )
    scallop_mm: float = Field(0.01, gt=0, description="Ball-nose scallop height.")
    resolution_mm: float = Field(0.05, gt=0.005, le=0.5, description="Heightmap cell size.")
    tolerance_mm: float = Field(0.005, gt=0, description="Point decimation tolerance.")


class CncOps(Strict):
    pocket: PocketOp = PocketOp()
    groove: GrooveOp = GrooveOp()
    drill: DrillOp = DrillOp()
    profile: ProfileOp = ProfileOp()
    face: FaceOp = FaceOp()
    rough3d: Rough3dOp = Rough3dOp()
    finish3d: Finish3dOp = Finish3dOp()


class AirPocket(Strict):
    """A glue-face air pocket: rounded rectangle in the side view, `depth_mm` into each half."""

    section: Literal["head", "tail"]
    x_mm: float
    z_mm: float
    length_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)


class CncConfig(Strict):
    """CNC halves (process.method = "cnc"): glue-face pockets, wire groove, setups, CAM."""

    rpm: float | None = Field(
        None,
        gt=0,
        description="Router rpm for the feeds; default: the body material's dial setting in "
        "the router's feeds.toml [rpm.dial] (Carbide 3D chart).",
    )
    corner_clearance_mm: float = Field(
        0.25, ge=0, description="Pocket corner radius = pocket tool radius + this."
    )
    ballast_max_length_mm: float = Field(
        30.0, gt=0, description="Longest glue-face ballast pocket before sizing fails."
    )
    ballast_min_depth_mm: float = Field(0.8, gt=0, description="Shallowest pocket per half.")
    ramp_angle_deg: float = Field(3.0, gt=0, le=30, description="Pocket/profile ramp entry.")
    cutout_overcut_mm: float = Field(0.3, ge=0, description="Cut-out depth below the stock.")
    climb: bool = True
    air_pockets: list[AirPocket] = []
    stock: CncStock = CncStock()
    registration: CncRegistration = CncRegistration()
    wire: CncWire = CncWire()
    tabs: CncTabs = CncTabs()
    ops: CncOps = CncOps()


class PrintConfig(Strict):
    """Print fab (`lure fab --format print`): filament slots, supports, pause (#35, #51).

    Slots are the tools of spec/printers/<machine>/machine.toml `[[filaments]]`.
    """

    body_slot: int | None = Field(
        None,
        ge=0,
        le=15,
        description="Tool (T<n>) for the body; default: first slot with process.material.",
    )
    interface_slot: int | None = Field(
        None,
        ge=0,
        le=15,
        description="Tool for the support interface; default: first slot with the machine's "
        "dissimilar pair (PETG↔PLA).",
    )
    supports: Literal["auto", "on", "off"] = Field(
        "auto", description="auto: loft3d always; profile25d by the belly roundover."
    )
    support_min_roundover_mm: float = Field(
        3.0, ge=0, description="profile25d auto: supports from this roundover radius up."
    )
    pause: Literal["cavity_ceiling", "none"] = Field(
        "cavity_ceiling",
        description="Insert the machine's pause before the layer that closes the cavity.",
    )
    overrides: dict[str, str | int | float | bool] = Field(
        {}, description="Extra slicer overrides (`key = value`, Orca CLI names), applied last."
    )


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
    internals: InternalsConfig = InternalsConfig()
    process: ProcessConfig
    dfm: DfmConfig = DfmConfig()
    cnc: CncConfig = CncConfig()
    print: PrintConfig = PrintConfig()
    report: ReportConfig = ReportConfig()
    solver: SolverConfig = SolverConfig()

    @property
    def is_resolved(self) -> bool:
        return (
            self.buoyancy.water_density is not None
            and self.ballast.density is not None
            and self.process.material_density is not None
            and all(h.mass_g is not None and h.density is not None for h in self.hardware)
        )
