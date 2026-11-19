"""Balance solver: finite-difference Jacobian plus a clamped least-squares step.

The solver is generic over a `Problem`:

- `evaluate(x)` is the expensive, exact model (a family build); it returns a `State`
  whose `vector` is a set of quantities that vary smoothly with the knobs (per-section
  mass and volume moments, pocket extents).
- `residuals(vector)` is cheap and exact: scaled target residuals from that vector.
- `jacobian(x, state)` returns d vector / d x. A problem fills columns analytically where it
  can (ballast mass and position need no geometry) and by finite differences elsewhere.

The solve **converges** when the returned pass meets every hard target (inside its band);
soft targets are minimized but may be infeasible together.

Each **pass** evaluates the model once, reports residuals, and takes a step: the state is
modelled as linear in the knobs (`vector + J·dx`), the residuals stay nonlinear, and a
bounded least-squares problem (`scipy.optimize.least_squares`) picks `dx` within the knob
bounds and a per-pass trust clamp. A small regularization keeps unconstrained knobs near
their starting values. The Jacobian is refreshed by Broyden updates and recomputed when a
step's actual gain falls well short of the predicted one.

Knobs are registered **per process** (`register_knobs`). Print knobs live in
`luregen.physics.balance`; CNC knobs (pocket depths, ballast, width trim) plug in the same
way once the CNC internals exist (F7b).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from luregen.physics.buoyancy import Residual, cost, hard_ok, weighted_vector

REGULARIZATION = 0.05  # weight on (x − x_start) / knob.scale
STALL_GAIN = 0.02  # stop when the predicted cost gain is below this fraction
STALL_ABS = 0.05  # … or below this absolute cost
REFRESH_RATIO = 0.25  # recompute the Jacobian when actual gain < this × predicted gain


class SolverError(RuntimeError):
    pass


@dataclass(frozen=True)
class Knob:
    """A tunable config value.

    `path` is the dotted config key. `lo`/`hi` are hard bounds, `step` the
    finite-difference step, `max_move` the largest change per pass, `scale` the typical
    magnitude used to normalize the regularization. `group` names the part of the model a
    geometry knob touches (e.g. a section): knobs in different groups are perturbed in the
    same finite-difference build. `analytic` knobs need no geometry for their column.
    """

    path: str
    lo: float
    hi: float
    step: float
    max_move: float
    scale: float = 1.0
    group: str | None = None
    analytic: bool = False
    unit: str = ""
    label: str = ""

    def clamp(self, v: float) -> float:
        return float(min(max(v, self.lo), self.hi))


KNOBS: dict[str, list[Knob]] = {}


def register_knobs(process: str, knobs: list[Knob]) -> None:
    """Register (or replace) the solver knobs for a process (`print`, `cnc`)."""
    KNOBS[process] = list(knobs)


def knobs_for(process: str) -> list[Knob]:
    return list(KNOBS.get(process, []))


class State(Protocol):
    vector: np.ndarray


class Problem(Protocol):
    knobs: list[Knob]

    def evaluate(self, x: np.ndarray) -> State: ...

    def residuals(self, vector: np.ndarray) -> list[Residual]: ...

    def jacobian(self, x: np.ndarray, state: State) -> np.ndarray: ...


@dataclass
class PassRecord:
    n: int
    x: dict[str, float]
    residuals: list[Residual]
    cost: float
    hard_ok: bool
    predicted_cost: float | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "pass": self.n,
            "knobs": {k: round(v, 4) for k, v in self.x.items()},
            "cost": round(self.cost, 5),
            "predicted_cost": None
            if self.predicted_cost is None
            else round(self.predicted_cost, 5),
            "hard_ok": self.hard_ok,
            "residuals": {r.id: r.as_dict() for r in self.residuals},
            "note": self.note,
        }


@dataclass
class SolveResult:
    x: np.ndarray
    state: Any
    residuals: list[Residual]
    passes: list[PassRecord]
    converged: bool
    reason: str
    jacobian_evals: int = 0
    warnings: list[str] = field(default_factory=list)


def solve(
    problem: Problem,
    x0: np.ndarray,
    *,
    max_passes: int = 5,
    on_pass: Callable[[int, int, str], None] | None = None,
) -> SolveResult:
    """Run up to `max_passes` model evaluations; return the best pass (hard targets first,
    then cost)."""
    knobs = problem.knobs
    lo = np.array([k.lo for k in knobs])
    hi = np.array([k.hi for k in knobs])
    x_start = np.clip(np.asarray(x0, dtype=float), lo, hi)
    x = x_start.copy()
    notify = on_pass or (lambda i, n, msg: None)

    notify(0, max_passes, "pass 1: evaluate the starting knobs")
    state = problem.evaluate(x)
    passes: list[PassRecord] = []
    best: tuple[np.ndarray, Any, list[Residual]] | None = None
    J: np.ndarray | None = None
    jac_evals = 0
    prev: tuple[np.ndarray, np.ndarray, float, float] | None = None  # x, vector, cost, pred
    converged, reason = False, "max passes"
    warnings: list[str] = []
    trust = 1.0  # multiplier on each knob's max_move
    fresh = False  # J was computed at the current x (no Broyden update since)

    for n in range(1, max_passes + 1):
        res = problem.residuals(state.vector)
        c = cost(res)
        rec = PassRecord(n, _named(knobs, x), res, c, hard_ok(res))
        passes.append(rec)
        if best is None or _better(res, best[2]):
            best = (x.copy(), state, res)

        # Broyden update from the last step, or a refresh when the model was poor
        if prev is not None and J is not None:
            x_prev, v_prev, c_prev, c_pred = prev
            dx, dv = x - x_prev, state.vector - v_prev
            if np.dot(dx, dx) > 0:
                J = J + np.outer(dv - J @ dx, dx) / np.dot(dx, dx)
                fresh = False
            gain, pred_gain = c_prev - c, c_prev - c_pred
            if pred_gain > 0 and gain < REFRESH_RATIO * pred_gain:
                J = None
                rec.note = "poor step prediction: Jacobian recomputed"
            if best[1] is not state:  # this pass is worse than an earlier one: go back
                x, state, res, c = best[0].copy(), best[1], best[2], cost(best[2])
                trust *= 0.5  # keep the Broyden-updated Jacobian; shorter steps
                rec.note = "worse than the best pass: back to it with half the step limit"

        # stop early only once the hard targets sit in their dead band: a mid that is
        # merely inside the band can leave it at a hardware band end
        if all(r.settled for r in res):
            converged, reason = True, "all targets met"
            break
        if J is None and n == max_passes:
            break  # no passes left to use a new Jacobian
        if J is None:
            notify(n, max_passes, f"pass {n}: Jacobian")
            J, fresh = problem.jacobian(x, state), True
            jac_evals += 1
        dx, c_pred = _step(problem, knobs, x, x_start, state.vector, J, lo, hi, trust)
        stalled = _stalled(c, c_pred, dx)
        if stalled and not hard_ok(res) and not fresh and n < max_passes:
            # the updated model sees no way out: rebuild it before giving up
            notify(n, max_passes, f"pass {n}: Jacobian (refresh)")
            J, fresh = problem.jacobian(x, state), True
            jac_evals += 1
            dx, c_pred = _step(problem, knobs, x, x_start, state.vector, J, lo, hi, trust)
            stalled = _stalled(c, c_pred, dx)
        rec.predicted_cost = c_pred
        if stalled and hard_ok(res):
            converged = True
            reason = "hard targets met; no further gain on the soft targets within the knob bounds"
            break
        if stalled:
            reason = "stalled: no step improves the hard targets within the knob bounds"
            break
        if n == max_passes:
            break
        prev = (x.copy(), state.vector.copy(), c, c_pred)
        x_new = np.clip(x + dx, lo, hi)
        notify(n, max_passes, f"pass {n + 1}: evaluate the stepped knobs")
        state, x_new = _evaluate_backtracking(problem, x, x_new, warnings)
        x = x_new

    # Convergence is judged on the residuals, not on the step: the solve has converged when
    # the returned pass meets every hard target inside its band. Soft targets are a
    # least-squares objective; when they conflict (see the feasibility notes) their residual
    # floor is not a convergence failure. Running out of passes with the hard targets met
    # only means the soft compromise may not be fully polished.
    if not converged and best is not None and hard_ok(best[2]):
        converged = True
        reason = (
            f"all targets met after {len(passes)} passes"
            if all(r.ok for r in best[2])
            else f"hard targets met; soft targets at their compromise after {len(passes)} passes"
        )
    bx, bstate, bres = best
    return SolveResult(bx, bstate, bres, passes, converged, reason, jac_evals, warnings)


def _stalled(c: float, c_pred: float, dx: np.ndarray) -> bool:
    gain = c - c_pred
    return gain <= max(STALL_GAIN * c, STALL_ABS) or bool(np.allclose(dx, 0, atol=1e-6))


def _evaluate_backtracking(problem: Problem, x: np.ndarray, x_new: np.ndarray, warnings):
    """Evaluate x_new; on a geometry failure halve the step (twice) before giving up."""
    from luregen.geometry.validate import GeometryError

    for attempt in range(3):
        try:
            return problem.evaluate(x_new), x_new
        except GeometryError as exc:
            warnings.append(f"solver step failed ({exc}); halving the step")
            if attempt == 2:
                raise SolverError(f"solver step keeps failing: {exc}") from exc
            x_new = x + 0.5 * (x_new - x)
    raise AssertionError("unreachable")


def _named(knobs: list[Knob], x: np.ndarray) -> dict[str, float]:
    return {k.path: float(v) for k, v in zip(knobs, x, strict=True)}


def _better(res: list[Residual], than: list[Residual]) -> bool:
    if hard_ok(res) != hard_ok(than):
        return hard_ok(res)
    return cost(res) < cost(than)


def _step(problem, knobs, x, x_start, vector, J, lo, hi, trust=1.0) -> tuple[np.ndarray, float]:
    """Bounded least squares on the linearized state; returns (dx, predicted cost)."""
    from scipy.optimize import least_squares

    scales = np.array([k.scale for k in knobs])
    move = trust * np.array([k.max_move for k in knobs])
    lo_s = np.maximum(lo, x - move)
    hi_s = np.minimum(hi, x + move)
    free = hi_s - lo_s > 1e-12
    if not free.any():
        return np.zeros_like(x), cost(problem.residuals(vector))

    def model(z_free: np.ndarray) -> np.ndarray:
        z = x.copy()
        z[free] = z_free
        res = problem.residuals(vector + J @ (z - x))
        reg = REGULARIZATION * (z - x_start) / scales
        return np.concatenate([weighted_vector(res), reg])

    fit = least_squares(
        model,
        np.clip(x, lo_s, hi_s)[free],
        bounds=(lo_s[free], hi_s[free]),
        x_scale=scales[free],
        method="dogbox",  # trf stalls when the start sits on a bound
        ftol=1e-10,
        xtol=1e-10,
    )
    z = x.copy()
    z[free] = fit.x
    pred = cost(problem.residuals(vector + J @ (z - x)))
    return z - x, pred
