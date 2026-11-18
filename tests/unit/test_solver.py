"""Solver convergence on synthetic problems (fast); the sample build test is in
test_balance_build.py (slow)."""

from dataclasses import dataclass

import numpy as np
import pytest

from luregen.physics.buoyancy import Residual
from luregen.physics.solver import Knob, knobs_for, register_knobs, solve


@dataclass
class S:
    vector: np.ndarray


class Toy:
    """state = f(x); residuals = (state − target) / scale. Counts evaluations."""

    def __init__(self, f, targets, knobs, hard=(True, True), weights=(10.0, 10.0)):
        self.f, self.targets, self.knobs = f, np.asarray(targets, float), knobs
        self.hard, self.weights = hard, weights
        self.evals = 0
        self.jacobians = 0

    def evaluate(self, x):
        self.evals += 1
        return S(np.asarray(self.f(x), float))

    def residuals(self, v):
        return [
            Residual(f"r{i}", "", float(v[i]), float(t), 1.0, w, h)
            for i, (t, w, h) in enumerate(zip(self.targets, self.weights, self.hard, strict=True))
        ]

    def jacobian(self, x, state):
        self.jacobians += 1
        J = np.zeros((len(state.vector), len(x)))
        for j in range(len(x)):
            xp = x.copy()
            xp[j] += 1e-4
            J[:, j] = (np.asarray(self.f(xp)) - state.vector) / 1e-4
        return J


def knob(name, lo=-10.0, hi=10.0, move=5.0):
    return Knob(name, lo=lo, hi=hi, step=0.1, max_move=move, scale=1.0)


def test_linear_problem_converges_in_two_passes():
    A = np.array([[2.0, 1.0], [0.5, -1.0]])
    toy = Toy(lambda x: A @ x, [4.0, -0.5], [knob("a"), knob("b")])
    out = solve(toy, np.zeros(2))
    assert out.converged and out.reason == "all targets met"
    assert len(out.passes) == 2
    assert np.allclose(out.x, np.linalg.solve(A, [4.0, -0.5]), atol=1e-2)


def test_nonlinear_problem_converges_within_five_passes():
    toy = Toy(
        lambda x: [x[0] ** 2 + 0.3 * x[1], np.sin(x[1]) + x[0]],
        [4.0, 2.5],
        [
            knob("a", 0.1, 5.0, 1.5),
            knob("b", -1.5, 1.5, 1.0),
        ],
    )
    out = solve(toy, np.array([1.0, 0.0]))
    assert out.converged
    assert len(out.passes) <= 5
    assert all(abs(r.scaled) <= 1.0 for r in out.residuals)


def test_bounds_clamp_and_report_not_converged():
    toy = Toy(lambda x: [x[0], x[1]], [20.0, 1.0], [knob("a", 0, 5), knob("b", -5, 5)])
    out = solve(toy, np.zeros(2), max_passes=3)
    assert not out.converged
    assert out.x[0] == pytest.approx(5.0)
    assert len(out.passes) <= 3
    assert out.residuals[1].ok


def test_soft_conflict_stops_when_hard_targets_hold():
    # two soft targets pulling one knob apart, one hard target on another knob
    toy = Toy(
        lambda x: [x[0], x[0], x[1]],
        [0.0, 6.0, 2.0],
        [knob("a"), knob("b")],
        hard=(False, False, True),
        weights=(1.0, 1.0, 10.0),
    )
    out = solve(toy, np.zeros(2))
    assert out.converged and "hard targets met" in out.reason
    assert out.x[0] == pytest.approx(3.0, abs=0.1)  # least-squares compromise
    assert out.x[1] == pytest.approx(2.0, abs=0.05)
    assert len(out.passes) <= 3


def test_on_pass_callback_and_registry():
    calls = []
    toy = Toy(lambda x: [x[0], x[1]], [1.0, 1.0], [knob("a"), knob("b")])
    solve(toy, np.zeros(2), on_pass=lambda i, n, msg: calls.append((i, n, msg)))
    assert calls and calls[0][0] == 0 and all(n == 5 for _, n, _ in calls)
    register_knobs("_test", [knob("z")])
    assert [k.path for k in knobs_for("_test")] == ["z"]
    assert knobs_for("nope") == []


def test_print_knobs_registered_and_cnc_open():
    import luregen.physics.balance  # noqa: F401  (registers the print knobs)

    paths = [k.path for k in knobs_for("print")]
    assert "ballast.mass_g" in paths and "internals.tail.keel_top_z_mm" in paths
    assert knobs_for("cnc") == []  # F7b registers the CNC knobs
