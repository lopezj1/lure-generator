"""Balance a family build: knob registry for print, the solver problem, and the report block.

`balance_build(cfg, table, envelope, family, emit)` is what `lure build` calls instead of
`family.build(...)`. It

1. sets the nominal trim putty fill (half the pocket capacity) when unset;
2. solves the registered knobs for the process unless the config is already solved
   (`solver.solved = true`, e.g. a resolved.toml), disabled, or has no knobs registered;
3. evaluates the hardware band ends with trim corrections and a feasibility analysis.

It returns the final config (solved values written in), the family build of the best pass
(exported as is, so the solve costs no extra build) and a `balance` report block.

**Cost model.** The envelope is built once and shared by every pass. Ballast mass and
position have analytic Jacobian columns (tungsten replacing solid plastic in the keel,
moved along the belly). Wall and keel knobs need geometry: head and tail knobs touch
disjoint sections, so one finite-difference build perturbs a head knob and a tail knob
together. A Jacobian costs 2 builds; Broyden updates reuse it across passes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import numpy as np

from luregen.physics.buoyancy import (
    Moments,
    Residual,
    Targets,
    TrimSpec,
    band_evaluation,
    feasibility,
    hardware_moments,
    lure_props_from_moments,
    residuals,
    section_moments,
)
from luregen.physics.massprops import SectionInput
from luregen.physics.solver import Knob, SolveResult, knobs_for, register_knobs, solve

if TYPE_CHECKING:
    from luregen.config import LureConfig
    from luregen.families import Emit, FamilyBuild, LureFamily
    from luregen.geometry.envelope import Envelope
    from luregen.profile.table import StationTable

# --- knob registry -------------------------------------------------------------------------

# `scale` sets how readily the least-squares step moves a knob (regularization toward the
# start values). Walls get a small scale so mass comes from keels and ballast first: a wall
# change acts on the whole cavity (and narrows the ballast pocket in the head), overlaps
# the keel's effect, and is the least linear knob.
PRINT_KNOBS = [
    Knob(
        "ballast.mass_g",
        lo=0.5,
        hi=60.0,
        step=1.0,
        max_move=15.0,
        scale=5.0,
        analytic=True,
        unit="g",
        label="Ballast mass",
    ),
    Knob(
        "ballast.x_from_joint_mm",
        lo=5.0,
        hi=60.0,
        step=1.0,
        max_move=10.0,
        scale=5.0,
        analytic=True,
        unit="mm",
        label="Ballast ahead of the joint",
    ),
    Knob(
        "internals.head.wall_thk_mm",
        lo=1.6,
        hi=5.0,
        step=0.4,
        max_move=0.75,
        scale=0.25,
        group="head",
        unit="mm",
        label="Head cavity wall",
    ),
    Knob(
        "internals.head.keel_top_z_mm",
        lo=-20.0,
        hi=10.0,
        step=1.5,
        max_move=6.0,
        scale=3.0,
        group="head",
        unit="mm",
        label="Head keel top z",
    ),
    Knob(
        "internals.tail.wall_thk_mm",
        lo=1.6,
        hi=5.0,
        step=0.4,
        max_move=0.75,
        scale=0.25,
        group="tail",
        unit="mm",
        label="Tail cavity wall",
    ),
    Knob(
        "internals.tail.keel_top_z_mm",
        lo=-20.0,
        hi=10.0,
        step=1.5,
        max_move=6.0,
        scale=3.0,
        group="tail",
        unit="mm",
        label="Tail keel top z",
    ),
]
register_knobs("print", PRINT_KNOBS)
# CNC knobs (pocket depths, ballast g and x, width trim) are registered with the CNC
# internals (F7b, #40): `register_knobs("cnc", [...])` plus a matching Problem.
register_knobs("cnc", [])

SECTIONS = ("head", "tail")
CLEARANCE_MARGIN_MM = 0.5  # the solver aims this far clear of the bosses
N_MOM = 8
EXTENT = 2 * N_MOM  # ballast pocket [x_lo, x_hi, z_top] follow the section moments


def get_path(cfg: Any, path: str) -> Any:
    obj = cfg
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def set_path(cfg: Any, path: str, value: Any) -> None:
    *head, last = path.split(".")
    obj = cfg
    for part in head:
        obj = getattr(obj, part)
    setattr(obj, last, value)


# --- problem --------------------------------------------------------------------------------


@dataclass
class GlideState:
    vector: np.ndarray
    cfg: LureConfig
    built: FamilyBuild


class PrintBalanceProblem:
    """Two-section print build; the state vector is [head moments, tail moments, ballast
    pocket x_lo, x_hi, z_top]."""

    def __init__(
        self,
        cfg: LureConfig,
        table: StationTable,
        envelope: Envelope,
        family: LureFamily,
        knobs: list[Knob],
    ):
        self.cfg, self.table, self.envelope, self.family = cfg, table, envelope, family
        self.knobs = knobs
        self.targets = Targets.from_config(cfg, table)
        self.builds = 0
        self.build_seconds = 0.0
        self.warnings: list[str] = []
        self.hardware: dict[str, Moments] = {}
        self.bosses: list[tuple[float, float]] = []
        self.boss_r = cfg.internals.boss_r_mm
        self.rho_body = float(cfg.process.material_density)
        self.rho_ballast = float(cfg.ballast.density)

    # model ----------------------------------------------------------------------------
    def config_at(self, x: np.ndarray) -> LureConfig:
        cfg = self.cfg.model_copy(deep=True)
        for k, v in zip(self.knobs, x, strict=True):
            set_path(cfg, k.path, round(float(v), 4))
        cfg.solver.solved_knobs = sorted({*cfg.solver.solved_knobs, *(k.path for k in self.knobs)})
        return cfg

    def evaluate(self, x: np.ndarray) -> GlideState:
        cfg = self.config_at(x)
        t0 = time.perf_counter()
        built = self.family.build(cfg, self.table, self.envelope, _quiet)
        self.build_seconds += time.perf_counter() - t0
        self.builds += 1
        if not self.hardware:
            self.hardware = hardware_moments(built.mass_inputs, "mid")
            internals = built.report.get("internals") or {}
            self.bosses = [tuple(b) for b in (internals.get("head") or {}).get("bosses", [])]
        return GlideState(self.state_vector(built), cfg, built)

    def state_vector(self, built: FamilyBuild) -> np.ndarray:
        mom = section_moments(built.mass_inputs)
        b = built.report["ballast"]
        x_lo = b["x_mm"] - 0.5 * b["length_mm"]
        x_hi = b["x_mm"] + 0.5 * b["length_mm"]
        z_top = b["z_axis_mm"] + b["r_mm"]
        return np.concatenate([mom["head"].vector(), mom["tail"].vector(), [x_lo, x_hi, z_top]])

    def residuals(self, vector: np.ndarray) -> list[Residual]:
        per = {
            name: Moments.from_vector(vector[i * N_MOM : (i + 1) * N_MOM]) + self.hardware[name]
            for i, name in enumerate(SECTIONS)
        }
        props = lure_props_from_moments(per, "mid")
        return residuals(props, self.targets, [self.clearance(vector[EXTENT:])])

    def clearance(self, extent: np.ndarray) -> Residual:
        """Ballast pocket vs screw-eye bosses (side view): the pocket rectangle
        [x_lo, x_hi] × [below the belly, z_top] must stay `boss_r` clear of each eye.

        The value is the smallest gap; the solver penalty is the root-sum-square of every
        boss's shortfall (smooth where two bosses squeeze the pocket from both ends, where
        a plain minimum has a kink that stalls Gauss–Newton)."""
        x_lo, x_hi, z_top = extent
        gaps = []
        for bx, bz in self.bosses:
            dx = max(x_lo - bx, 0.0, bx - x_hi)
            dz = max(bz - z_top, 0.0)
            gaps.append(float(np.hypot(dx, dz)) - self.boss_r)
        gap = min(gaps, default=0.0)
        short = [min(g - CLEARANCE_MARGIN_MM, 0.0) for g in gaps]
        return Residual(
            "ballast_clearance",
            "Ballast pocket clear of screw-eye bosses",
            gap,
            0.0,
            1.0,
            self.targets.weights["ballast_clearance"],
            True,
            side="min",
            unit="mm",
            margin=CLEARANCE_MARGIN_MM,
            penalty_value=-float(np.sqrt(np.sum(np.square(short)))),
        )

    # Jacobian ----------------------------------------------------------------------------
    def jacobian(self, x: np.ndarray, state: GlideState) -> np.ndarray:
        n_state = len(state.vector)
        J = np.zeros((n_state, len(self.knobs)))
        for j, k in enumerate(self.knobs):
            if k.analytic:
                J[:, j] = self.analytic_column(k, state)
        # geometry knobs: one build per round, one knob per group in each round
        groups: dict[str, list[int]] = {}
        for j, k in enumerate(self.knobs):
            if not k.analytic:
                groups.setdefault(k.group or f"_{j}", []).append(j)
        rounds = max((len(v) for v in groups.values()), default=0)
        for r in range(rounds):
            cols = [js[r] for js in groups.values() if r < len(js)]
            self._fd_round(x, state, cols, J)
        return J

    def _fd_round(self, x, state, cols: list[int], J: np.ndarray) -> None:
        for sign in (1.0, -1.0):
            xp = x.copy()
            for j in cols:
                k = self.knobs[j]
                h = sign * k.step
                if not k.lo <= x[j] + h <= k.hi:
                    h = -h
                xp[j] = x[j] + h
            try:
                pert = self.evaluate(xp)
            except Exception as exc:  # GeometryError, FamilyError: try the other side
                self.warnings.append(f"finite-difference build failed ({exc})")
                continue
            dv = pert.vector - state.vector
            for j in cols:
                idx = self._group_indices(self.knobs[j].group)
                J[idx, j] = dv[idx] / (xp[j] - x[j])
            return
        names = ", ".join(self.knobs[j].path for j in cols)
        self.warnings.append(f"no finite-difference column for {names}: frozen this pass")

    @staticmethod
    def _group_indices(group: str | None) -> np.ndarray:
        if group == "head":  # the head wall also sizes the ballast pocket radius
            return np.r_[0:N_MOM, EXTENT : EXTENT + 3]
        if group == "tail":
            return np.r_[N_MOM:EXTENT]
        return np.r_[0 : EXTENT + 3]

    def analytic_column(self, k: Knob, state: GlideState) -> np.ndarray:
        """Tungsten replacing solid plastic in the head keel."""
        from luregen.geometry.pockets import belly_z

        b = state.built.report["ballast"]
        col = np.zeros(len(state.vector))
        c = np.array(b["centroid_mm"], dtype=float)
        vol_mm3 = 1000.0 * b["volume_cm3"]
        area = vol_mm3 / max(b["length_mm"], 1e-6)
        if k.path == "ballast.mass_g":  # per g
            dm = 1.0 - self.rho_body / self.rho_ballast
            col[0:N_MOM] = Moments.point(dm, c).vector()
            dlen = (1000.0 / self.rho_ballast) / area
            col[EXTENT : EXTENT + 3] = [-0.5 * dlen, 0.5 * dlen, 0.0]
        elif k.path == "ballast.x_from_joint_mm":  # per mm forward (−x)
            net = b["mass_g"] - self.rho_body * b["volume_cm3"]
            slope = belly_z(self.table, b["x_mm"] + 0.5) - belly_z(self.table, b["x_mm"] - 0.5)
            col[0:N_MOM] = Moments(0.0, (-net, 0.0, -net * slope)).vector()
            col[EXTENT : EXTENT + 3] = [-1.0, -1.0, -slope]
        return col


def _quiet(stage: str, frac: float, msg: str) -> None:
    pass


# --- orchestration --------------------------------------------------------------------------


@dataclass
class BalanceOutcome:
    cfg: LureConfig
    built: FamilyBuild
    report: dict[str, Any]


def nominal_trim_fill(cfg: LureConfig) -> LureConfig:
    """Set `internals.trim_fill_g` to half the trim capacity (headroom both ways)."""
    if not cfg.buoyancy.trim_pocket or cfg.internals.trim_fill_g is not None:
        return cfg
    from luregen.families.glide import hardware_band_g

    out = cfg.model_copy(deep=True)
    capacity = cfg.internals.trim_capacity_g or hardware_band_g(cfg) + 1.0
    out.internals.trim_fill_g = round(0.5 * capacity, 3)
    return out


def solver_knobs(cfg: LureConfig) -> tuple[list[Knob], list[str]]:
    """Registered knobs for the process, bounded by the DFM rules; minus user pins."""
    pinned = []
    out = []
    for k in knobs_for(cfg.process.method):
        user_pin = k.path not in cfg.solver.solved_knobs
        if k.path == "ballast.mass_g" and cfg.ballast.mass_g is not None and user_pin:
            pinned.append(k.path)
            continue
        if k.path.endswith("wall_thk_mm"):
            k = replace(k, lo=max(k.lo, cfg.dfm.print.min_wall_mm))
        out.append(k)
    return out, pinned


def start_values(cfg: LureConfig, knobs: list[Knob]) -> np.ndarray:
    vals = []
    for k in knobs:
        v = get_path(cfg, k.path)
        if v is None and k.path == "ballast.mass_g":
            v = cfg.internals.ballast_seed_g
        vals.append(k.clamp(float(v)))
    return np.array(vals, dtype=float)


def balance_build(
    cfg: LureConfig,
    table: StationTable,
    envelope: Envelope,
    family: LureFamily,
    emit: Emit,
) -> BalanceOutcome:
    t0 = time.perf_counter()
    cfg = nominal_trim_fill(cfg)
    knobs, pinned = solver_knobs(cfg)
    skip = None
    if not cfg.solver.enabled:
        skip = "solver disabled (solver.enabled = false)"
    elif cfg.solver.solved:
        skip = "config already solved (solver.solved = true); set it false to solve again"
    elif not knobs:
        skip = f"no solver knobs registered for process {cfg.process.method!r} yet"

    solver_block: dict[str, Any]
    if skip:
        emit("joint", 0.0, "family build")
        built = family.build(cfg, table, envelope, emit)
        solver_block = {"status": "skipped", "reason": skip, "pinned": pinned}
    else:
        problem = PrintBalanceProblem(cfg, table, envelope, family, knobs)
        x0 = start_values(cfg, knobs)

        def on_pass(i: int, n: int, msg: str) -> None:
            emit("solve", i / n, msg)

        result = solve(problem, x0, max_passes=cfg.solver.max_passes, on_pass=on_pass)
        emit("solve", 1.0, f"solved in {len(result.passes)} passes")
        cfg = result.state.cfg.model_copy(deep=True)
        cfg.solver.solved = True
        built = result.state.built
        solver_block = _solver_block(problem, result, x0, pinned, time.perf_counter() - t0)

    targets = Targets.from_config(cfg, table)
    trim = TrimSpec.from_report(built.report.get("trim_pocket"))
    props_mid = lure_props_from_moments(_with_hw(built.mass_inputs, "mid"), "mid")
    report = {
        "solver": solver_block,
        "targets": {
            "water": targets.water,
            "water_density": targets.water_density,
            "sink_mode": targets.sink_mode,
            "sink_band": list(targets.sink),
            "weights": targets.weights,
        },
        "residuals": [r.as_dict() for r in residuals(props_mid, targets)],
        "hardware_band": band_evaluation(built.mass_inputs, targets, trim),
        "feasibility": feasibility(props_mid, targets),
        "runtime_s": round(time.perf_counter() - t0, 2),
    }
    return BalanceOutcome(cfg, built, report)


def _with_hw(sections: list[SectionInput], band) -> dict[str, Moments]:
    parts = section_moments(sections)
    hw = hardware_moments(sections, band)
    return {k: parts[k] + hw[k] for k in parts}


def _solver_block(
    problem: PrintBalanceProblem, result: SolveResult, x0: np.ndarray, pinned, seconds: float
) -> dict:
    knobs = {}
    for k, a, b in zip(problem.knobs, x0, result.x, strict=True):
        knobs[k.path] = {
            "label": k.label,
            "unit": k.unit,
            "start": round(float(a), 4),
            "solved": round(float(b), 4),
            "bounds": [k.lo, k.hi],
            "at_bound": bool(abs(b - k.lo) < 1e-6 or abs(b - k.hi) < 1e-6),
        }
    return {
        "status": "converged" if result.converged else "not_converged",
        "reason": result.reason,
        "passes": [p.as_dict() for p in result.passes],
        "n_passes": len(result.passes),
        "builds": problem.builds,
        "jacobians": result.jacobian_evals,
        "build_seconds": round(problem.build_seconds, 2),
        "seconds": round(seconds, 2),
        "knobs": knobs,
        "pinned": pinned,
        "final": {r.id: r.as_dict() for r in result.residuals},
        "warnings": [*problem.warnings, *result.warnings],
    }
