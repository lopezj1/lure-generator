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
    length_mm: float | None = Field(
        None, ge=100, le=250, description="Nose-to-tail length; set this or the target weight."
    )
    target_weight_oz: float | None = Field(
        None, gt=0, description="Target weight; the build solves the length to hit it."
    )
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
    mode: Literal["ratio_of_depth"] = Field(
        "ratio_of_depth", description="How width follows depth (ratio of depth only, for now)."
    )
    value: float = Field(0.34, gt=0, le=1.5, description="Body width ÷ depth at every station.")


class Smoothing(Strict):
    method: Literal["efd", "spline"] = Field(
        "efd", description="Outline smoothing: elliptic Fourier (efd) or smoothing spline."
    )
    harmonics: int = Field(20, ge=4, le=60, description="EFD harmonics kept; fewer = smoother.")
    spline_s: float | None = Field(
        None, description="Spline smoothing factor; default: one px² per outline point."
    )


PAPER_SIZES_MM = {"letter": (215.9, 279.4), "a4": (210.0, 297.0)}  # short × long side


class PaperSheet(Strict):
    """Sheet of paper under the subject for `profile.scale = "paper"` (or `auto`)."""

    size: Literal["letter", "a4", "custom"] = Field(
        "letter", description="Paper size: US Letter (8.5 × 11 in), A4, or custom."
    )
    width_mm: float | None = Field(None, gt=0, description="Custom sheet width (mm).")
    height_mm: float | None = Field(None, gt=0, description="Custom sheet height (mm).")
    inset_mm: float = Field(
        4.0, ge=0, le=30, description="Border of the sheet ignored when segmenting."
    )

    @model_validator(mode="after")
    def custom_size(self) -> "PaperSheet":
        if self.size == "custom" and (self.width_mm is None or self.height_mm is None):
            raise ValueError("profile.paper.size = 'custom' requires width_mm and height_mm")
        return self

    def dims_mm(self) -> tuple[float, float]:
        """(short, long) side in mm."""
        if self.size == "custom":
            return (min(self.width_mm, self.height_mm), max(self.width_mm, self.height_mm))
        return PAPER_SIZES_MM[self.size]


class ProfileConfig(Strict):
    source: Literal["image", "preset"] = Field(
        description="Trace the outline from a photo (image) or load a station table (preset)."
    )
    image: Path | None = Field(None, description="Side-view photo of the bait.")
    top_image: Path | None = Field(
        None, description="Optional top-view photo; sets the width instead of the width rule."
    )
    preset: Path | None = Field(None, description="Station table CSV from an earlier trace.")
    scale: Literal["aruco", "paper", "length", "auto"] = Field(
        "length",
        description="Photo scale: ArUco marker, paper sheet, known length, or auto (tries each).",
    )
    aruco_marker_mm: float = Field(
        30.0, gt=0, description="Side length of the printed ArUco marker in the photo."
    )
    paper: PaperSheet = Field(PaperSheet(), description="Paper sheet under the subject (scale).")
    n_stations: int = Field(23, ge=8, le=30, description="Cross-sections along the body loft.")
    section_shape: Literal["ellipse", "egg", "teardrop"] = Field(
        "ellipse", description="Cross-section shape: ellipse, egg (wide belly) or teardrop."
    )
    width_rule: WidthRule = Field(WidthRule(), description="Body width as a ratio of depth.")
    smoothing: Smoothing = Field(Smoothing(), description="Smoothing of the traced outline.")

    @model_validator(mode="after")
    def source_file(self) -> "ProfileConfig":
        if self.source == "image" and self.image is None:
            raise ValueError("profile.source = 'image' requires profile.image")
        if self.source == "preset" and self.preset is None:
            raise ValueError("profile.source = 'preset' requires profile.preset")
        return self


class JointConfig(Strict):
    joint_frac: float = Field(
        0.50,
        ge=0.45,
        le=0.55,
        description="Head/tail split, as a fraction of length from the nose.",
    )
    gap_mm: float = Field(2.3, ge=1.5, le=3.5, description="Gap between head and tail faces.")
    angle_deg: float = Field(
        37.0, ge=30, le=45, description="V arm angle from the lateral (y) axis."
    )
    hinge_eye_z: tuple[float, float] = Field(
        (-12.0, 6.0), description="Heights (z) of the two head hinge eyes the pin passes through."
    )
    line_tie_z: float = Field(-4.0, ge=-6, le=0, description="Line-tie eye height (z) on the nose.")
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
    water: Literal["salt", "fresh"] = Field("salt", description="Water the lure is tuned for.")
    water_density: float | None = Field(
        None, description="Water density (from the water type)."
    )  # filled by resolve()
    target_sink: Literal["float", "suspend", "slow_sink"] = Field(
        "slow_sink", description="Behaviour at rest: float, suspend or slow sink."
    )
    sink_bands: dict[Literal["float", "suspend", "slow_sink"], Range] = Field(
        description="Overall density range [min, max] for each sink behaviour."
    )
    # The head density is derived (sink band, tail density and head:tail ratio fix it), not
    # a target (#96). Configs and resolved.toml files from before that still load.
    tail_density: Range = Field(
        (0.70, 0.80), description="Tail density range; a light tail swings and kicks."
    )
    head_tail_mass_ratio: float = Field(2.4, gt=0, description="Head mass ÷ tail mass target.")
    level_tolerance_mm: float = Field(
        1.0, gt=0, description="How far the CG may sit off the line tie and still hang level."
    )
    cb_above_cg_frac: float = Field(
        0.08, ge=0, description="Buoyancy centre above the CG, × body depth (self-righting)."
    )
    trim_pocket: bool = Field(
        True, description="Tail pocket for tungsten putty to fine-tune the balance."
    )

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
    material: str = Field("tungsten", description="Ballast material (spec/materials.toml).")
    density: float | None = Field(None, description="Ballast density (catalog).")
    mass_g: float | None = Field(None, gt=0, description="Pin the ballast mass; else solved.")
    x_from_joint_mm: float = Field(22.0, description="Ballast pin centre ahead of the joint.")
    z_above_belly_mm: float = Field(6.0, description="Ballast pin centre above the belly.")


class HardwareItem(Strict):
    """A hardware point mass. `id` refers to spec/hardware; unset fields come from the catalog."""

    id: str = Field(description="Part id in spec/hardware (fills name, mass, material).")
    section: Literal["head", "tail"] = Field(description="Section the part is attached to.")
    x_mm: float = Field(description="Position from the nose tip.")
    z_mm: float = Field(description="Height above the nose axis.")
    qty: int = Field(1, ge=1, description="Number of parts at this spot.")
    name: str | None = Field(None, description="Display name (catalog).")
    mass_g: Range | None = Field(None, description="Mass range [min, max] (catalog).")
    material: str | None = Field(None, description="Material (catalog).")
    density: float | None = Field(None, description="Density (catalog).")
    measured: bool | None = Field(None, description="Mass was weighed, not estimated.")


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
    trim_material: str = Field("tungsten_putty", description="Trim putty material.")
    trim_fill_g: float | None = Field(
        None,
        ge=0,
        description="Nominal (hardware band mid) putty fill; the solver sets half the capacity.",
    )
    trim_density: float | None = Field(
        None, description="Trim putty density (catalog)."
    )  # filled by resolve()


class SolverConfig(Strict):
    """Balance solver (F4). `lure build` writes `solved = true` and the solved knob values
    into resolved.toml, so rebuilding it reproduces the lure without solving again; pass
    `--set solver.solved=false` to solve again."""

    enabled: bool = Field(True, description="Solve ballast, walls and trim to hit the targets.")
    max_passes: int = Field(5, ge=1, le=10, description="Solver passes before it gives up.")
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
    method: Literal["print", "cnc"] = Field(
        description="Print it, or cut two wood halves on the router and glue them."
    )
    body_style: Literal["loft3d", "profile25d"] = Field(
        "loft3d", description="loft3d: round body; profile25d: flat sides, rounded edges."
    )
    material: str = Field(description="Body material: filament or wood (spec/materials.toml).")
    material_density: float | None = Field(
        None, gt=0, description="Override, e.g. a weighed blank."
    )
    rigging: Literal["screw_eye", "through_wire"] = Field(
        "screw_eye", description="Screw eyes, or a wire through the body (CNC groove)."
    )


class PrintDfm(Strict):
    """Print DFM thresholds (F5); the part is printed upright, z up."""

    min_wall_mm: float = Field(1.6, description="Thinnest printed wall allowed.")
    cavity_roof: Literal["gable45", "teardrop"] = Field(
        "gable45", description="Cavity ceiling shape (both print without support)."
    )
    min_skin_to_cavity_mm: float = Field(
        1.2, description="Thinnest skin allowed between the outside and a cavity."
    )
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
    tools: list[str] = Field(
        ["flat_6.35", "flat_3.175", "ball_3.175"],
        description="Tools the checks assume (tools.toml).",
    )
    min_wood_wall_mm: float = Field(3.5, description="Thinnest wood wall allowed.")
    max_pocket_depth_x_dia: float = Field(
        3.0, description="Deepest pocket allowed, in tool diameters."
    )
    roundover_r_mm: float = Field(
        4.0, description="Rim roundover the 3D finish cuts on each half's outer edge."
    )
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
    print: PrintDfm = Field(PrintDfm(), description="Printability checks.")
    cnc: CncDfm = Field(CncDfm(), description="Router checks: wall thickness, reach, pockets.")


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

    enabled: bool = Field(True, description="Drill flip-pin holes to line up setup 2.")
    pin_d_mm: float = Field(6.35, gt=0, description="Flip-pin dowel diameter (1/4in).")
    into_spoilboard_mm: float = Field(
        3.0, ge=0, description="Flip-pin hole depth into the spoilboard."
    )


class CncWire(Strict):
    """Through-wire half-groove (process.rigging = "through_wire"): a square channel."""

    diameter_mm: float = Field(1.04, gt=0, description="Wire diameter (0.041in = 1.04 mm).")
    clearance_mm: float = Field(0.15, ge=0, description="Extra depth per half.")
    z_mm: float | None = Field(None, description="Groove height; default: joint.line_tie_z.")
    exit_mm: float = Field(3.0, ge=0, description="Groove run-out past the outline.")


class CncTabs(Strict):
    count: int = Field(4, ge=0, le=8, description="Holding tabs per half on the cut-out.")
    width_mm: float = Field(5.0, gt=0, description="Tab length along the cut-out.")
    height_mm: float = Field(1.5, gt=0, description="Tab height above the cut-out floor.")


TOOL_HELP = "Tool id in the router's tools.toml."
MODE_HELP = "Feeds calculator: conservative or aggressive (unless a preset is set)."
PRESET_HELP = "Named feeds preset in feeds.toml (overrides the calculator)."


class CncOp(Strict):
    """Tool and feeds for one CAM operation: a calculator mode or a named preset."""

    tool: str = Field(description=TOOL_HELP)
    mode: Literal["conservative", "aggressive"] = Field("conservative", description=MODE_HELP)
    preset: str | None = Field(None, description=PRESET_HELP)


# One class per op so each default lives on the field itself (the web editor drops values
# equal to the defaults key by key, so nested defaults must not differ from the class's).
class PocketOp(CncOp):
    tool: str = Field("flat_3.175", description=TOOL_HELP)
    mode: Literal["conservative", "aggressive"] = Field("aggressive", description=MODE_HELP)


class GrooveOp(CncOp):
    # The pocket's 1/8in flat cuts a square channel in the same tool run (no tool change).
    tool: str = Field("flat_3.175", description=TOOL_HELP)


class DrillOp(CncOp):
    tool: str = Field("flat_3.175", description=TOOL_HELP)


class ProfileOp(CncOp):
    tool: str = Field("flat_3.175", description=TOOL_HELP)
    preset: str | None = Field("owner_contour_flat_3.175", description=PRESET_HELP)


class FaceOp(CncOp):
    tool: str = Field("flat_3.175", description=TOOL_HELP)
    mode: Literal["conservative", "aggressive"] = Field("aggressive", description=MODE_HELP)


class Rough3dOp(CncOp):
    """Setup-2 z-level roughing of the outer face (#42)."""

    tool: str = Field("flat_3.175", description=TOOL_HELP)
    preset: str | None = Field("owner_rough_flat_3.175", description=PRESET_HELP)
    enabled: bool = Field(True, description="Rough the outer face before finishing.")
    allowance_mm: float = Field(0.5, ge=0, description="Stock left for finishing.")
    max_stair_mm: float = Field(
        0.25, gt=0, description="Extra rest levels so shallow areas keep ≤ this stair."
    )
    resolution_mm: float = Field(0.15, gt=0.01, le=1.0, description="Heightmap cell size.")
    boundary_mm: float = Field(
        1.0, ge=0, description="Tool centres run this far past silhouette + r + allowance."
    )


class Finish3dOp(CncOp):
    """Setup-2 parallel raster finishing of the outer face (#42). Default: the owner's R1.0
    tapered ball at the scallop stepover √(8·R·h); a flat uses the preset's stepover."""

    tool: str = Field("taperball_2", description=TOOL_HELP)
    preset: str | None = Field("owner_finish_taperball_2", description=PRESET_HELP)
    enabled: bool = Field(True, description="Finish the outer face (3D raster).")
    stepover_mm: float | None = Field(
        None, gt=0, description="Override; default: the preset (flat) or the scallop (ball)."
    )
    scallop_mm: float = Field(0.01, gt=0, description="Ball-nose scallop height.")
    resolution_mm: float = Field(0.05, gt=0.005, le=0.5, description="Heightmap cell size.")
    tolerance_mm: float = Field(0.005, gt=0, description="Point decimation tolerance.")


class CncOps(Strict):
    pocket: PocketOp = Field(PocketOp(), description="Glue-face cavity and ballast pockets.")
    groove: GrooveOp = Field(GrooveOp(), description="Through-wire channel in the glue face.")
    drill: DrillOp = Field(DrillOp(), description="Hinge, eye and flip-pin holes.")
    profile: ProfileOp = Field(ProfileOp(), description="Cut-out around each half.")
    face: FaceOp = Field(FaceOp(), description="Facing of the glue face (setup 1).")
    rough3d: Rough3dOp = Field(Rough3dOp(), description="Z-level roughing of the outer face.")
    finish3d: Finish3dOp = Field(Finish3dOp(), description="Raster finishing of the outer face.")


class AirPocket(Strict):
    """A glue-face air pocket: rounded rectangle in the side view, `depth_mm` into each half."""

    section: Literal["head", "tail"] = Field(description="Section the pocket is in.")
    x_mm: float = Field(description="Pocket centre from the nose tip.")
    z_mm: float = Field(description="Pocket centre height.")
    length_mm: float = Field(gt=0, description="Pocket length (x).")
    height_mm: float = Field(gt=0, description="Pocket height (z).")
    depth_mm: float = Field(gt=0, description="Pocket depth into each half.")


class CncConfig(Strict):
    """CNC halves (process.method = "cnc"): glue-face pockets, wire groove, setups, CAM."""

    rpm: float | None = Field(
        None,
        gt=0,
        description="Router rpm for feeds; default: the material's dial setting (feeds.toml).",
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
    climb: bool = Field(True, description="Climb milling; false = conventional.")
    air_pockets: list[AirPocket] = Field(
        [], description="Extra glue-face pockets that lighten a section."
    )
    stock: CncStock = Field(CncStock(), description="Board size and layout of the halves.")
    registration: CncRegistration = Field(
        CncRegistration(), description="Flip pins that line the board up for setup 2."
    )
    wire: CncWire = Field(CncWire(), description="Through-wire groove (rigging = through_wire).")
    tabs: CncTabs = Field(
        CncTabs(), description="Tabs that hold each half in the board during the cut-out."
    )
    ops: CncOps = Field(CncOps(), description="Tool and feeds for each CAM operation.")


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


class FinSlot(Strict):
    """Vertical slot in the tail's cut end (centred on y = 0, in the fin's plane) that takes
    the fin's root tab. The tab is the fin's thickness; the slot adds the fit clearance."""

    depth_mm: float = Field(10.0, gt=0, le=40, description="Slot depth into the tail (x).")
    height_mm: float | None = Field(
        None, gt=0, description="Slot height (z); default: 50 % of the fin root height."
    )
    clearance_mm: float = Field(
        0.3, ge=0, le=1.5, description="Fit clearance on the tab thickness and height."
    )
    end_gap_mm: float = Field(
        0.5, ge=0, le=3, description="Tab stops this short of the slot bottom (glue room)."
    )
    corner_r_mm: float = Field(
        1.0,
        ge=0,
        description="Slot corner radius in the side view (CNC: at least the pocket tool's).",
    )


class FinScrew(Strict):
    """Lateral screw (along y) through the tail and the fin tab. `id` refers to
    spec/hardware; mass, material and density are filled from the catalog."""

    id: str = Field("screw_m2x6_pan", description="Screw id in spec/hardware.")
    x_from_cut_mm: float | None = Field(
        None, gt=0, description="Screw axis ahead of the tail cut; default: mid-slot."
    )
    z_offset_mm: float = Field(0.0, description="Screw axis above the fin centre line.")
    pilot_d_mm: float = Field(
        1.6, gt=0, le=5, description="Hole through the tail (self-tapping pilot)."
    )
    tab_hole_d_mm: float = Field(1.6, gt=0, le=5, description="Hole through the fin tab.")
    length_mm: float = Field(6.0, gt=0, le=30, description="Screw length under the head.")
    name: str | None = Field(None, description="Display name (catalog).")
    mass_g: Range | None = Field(None, description="Mass range [min, max] (catalog).")
    material: str | None = Field(None, description="Material (catalog).")
    density: float | None = Field(None, description="Density (catalog).")
    measured: bool | None = Field(None, description="Mass was weighed, not estimated.")


class FinConfig(Strict):
    """Separate tail fin (#123): a flat paddle that replaces the pointed tail tip, printed on
    its own in a flexible material. Its root tab slides into a slot in the tail's cut end and
    a lateral screw through the tail and the tab holds it.

    Side view (x, z): a trapezoid from the root (`root_height_mm` tall at the tail cut) to
    the trailing edge (`span_mm` tall), with an optional V fork in the trailing edge and
    rounded trailing corners; `thickness_mm` thick in y. Lengths that place or size it on
    the body scale with a weight-driven length; thicknesses, clearances and the screw don't.
    """

    material: str = Field("TPU", description="Fin material (spec/materials.toml).")
    density: float | None = Field(None, gt=0, description="Override, e.g. a weighed fin.")
    cut_from_tail_mm: float = Field(
        20.0, gt=0, description="Tail tip length replaced by the fin (the tail is cut here)."
    )
    length_mm: float = Field(30.0, gt=0, description="Fin length along x, root to trailing edge.")
    root_height_mm: float | None = Field(
        None, gt=0, description="Root height (z); default: 80 % of the tail depth at the cut."
    )
    span_mm: float = Field(36.0, gt=0, description="Trailing-edge height (z).")
    thickness_mm: float = Field(
        2.4, gt=0, description="Fin and tab thickness (y); the fin prints flat on its side."
    )
    fork_depth_mm: float = Field(
        0.0, ge=0, description="V notch depth into the trailing edge (0 = straight paddle)."
    )
    corner_r_mm: float = Field(2.0, ge=0, description="Trailing-corner rounding radius.")
    slot: FinSlot = Field(FinSlot(), description="Slot in the tail's cut end for the fin's tab.")
    screw: FinScrew = Field(FinScrew(), description="Screw through the tail and the fin tab.")


class ReportConfig(Strict):
    show_oz: bool = Field(True, description="Show ounces next to grams in reports.")


class LureConfig(Strict):
    name: str = Field(description="Variant name; also names the build folder.")
    family: str = Field(description="Bait family (spec/designs/<family>).")
    size: SizeConfig = Field(description="Body length, or a target weight to solve it from.")
    profile: ProfileConfig = Field(description="Side outline (photo or preset) and cross-sections.")
    joint: JointConfig | None = Field(None, description="Head–tail hinge: split point and pin.")
    fin: FinConfig | None = Field(
        None, description="Separate printed tail fin, slotted and screwed (unset: solid tail)."
    )
    buoyancy: BuoyancyConfig = Field(
        description="Water, sink target and balance the solver aims for."
    )
    ballast: BallastConfig = Field(BallastConfig(), description="Ballast pin in the head.")
    hardware: list[HardwareItem] = Field(
        [], description="Eyes, hooks, rings and pins as point masses (spec/hardware)."
    )
    internals: InternalsConfig = Field(
        InternalsConfig(), description="Printed cavities, ballast/trim pockets and eye bosses."
    )
    process: ProcessConfig = Field(description="Print or CNC, body style, material and rigging.")
    dfm: DfmConfig = Field(DfmConfig(), description="Manufacturability checks.")
    cnc: CncConfig = Field(CncConfig(), description="Router job: stock, pockets, setups and CAM.")
    print: PrintConfig = Field(
        PrintConfig(), description="Printer job: filament slots and supports."
    )
    report: ReportConfig = Field(ReportConfig(), description="Build report options.")
    solver: SolverConfig = Field(SolverConfig(), description="Balance solver settings.")

    @property
    def is_resolved(self) -> bool:
        return (
            self.buoyancy.water_density is not None
            and self.ballast.density is not None
            and self.process.material_density is not None
            and all(h.mass_g is not None and h.density is not None for h in self.hardware)
            and (
                self.fin is None
                or (self.fin.density is not None and self.fin.screw.density is not None)
            )
        )
