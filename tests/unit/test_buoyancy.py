import numpy as np
import pytest

from luregen.physics.buoyancy import (
    Moments,
    Residual,
    Targets,
    TrimSpec,
    band_evaluation,
    feasibility,
    lure_props_from_moments,
    putty_to_density,
    residuals,
    section_moments,
)
from luregen.physics.massprops import Part, PointMass, SectionInput, lure_props
from luregen.physics.scaling import length_for_weight, scale_config


def targets(**kw) -> Targets:
    base = dict(
        water="salt",
        water_density=1.025,
        sink_mode="slow_sink",
        sink=(1.03, 1.06),
        tail_density=(0.70, 0.80),
        ratio=2.4,
        level_tol=1.0,
        righting_mm=2.0,
    )
    return Targets(**{**base, **kw})


def sections(hw_lo=1.0, hw_hi=2.0) -> list[SectionInput]:
    head = SectionInput(
        "head",
        [
            Part("body", 20_000.0, (40.0, 0.0, -2.0), 1.27),
            Part("cavity", 20_000.0, (40.0, 0.0, 4.0), 0.0),
            Part("ballast", 1_000.0, (50.0, 0.0, -10.0), 18.0),
        ],
        [PointMass("hook", (hw_lo, hw_hi), (45.0, 0.0, -15.0), 7.85)],
    )
    tail = SectionInput(
        "tail",
        [
            Part("body", 12_000.0, (110.0, 0.0, -1.0), 1.27),
            Part("cavity", 14_000.0, (110.0, 0.0, 3.0), 0.0),
        ],
        [PointMass("hook", (hw_lo, hw_hi), (150.0, 0.0, -10.0), 7.85)],
    )
    return [head, tail]


def test_moments_match_massprops():
    secs = sections()
    ref = lure_props(secs, "mid")
    hw = {s.name: Moments.from_hardware(s.hardware, "mid") for s in secs}
    per = {k: m + hw[k] for k, m in section_moments(secs).items()}
    got = lure_props_from_moments(per, "mid")
    assert got.lure.mass_g == pytest.approx(ref.lure.mass_g)
    assert got.lure.volume_mm3 == pytest.approx(ref.lure.volume_mm3)
    assert np.allclose(got.lure.cg, ref.lure.cg) and np.allclose(got.lure.cb, ref.lure.cb)
    assert got.ratio() == pytest.approx(ref.ratio())
    v = per["head"].vector()
    assert Moments.from_vector(v) == per["head"]
    assert (per["head"] - per["head"]).mass == 0.0


def test_putty_to_density_is_exact():
    m = Moments.point(60.0, (80, 0, 0), 60_000.0)  # 1.0 g/cm³
    d = putty_to_density(m, 1.04, 10.0)
    new = m + Moments.point(d, (140, 0, 0), 1000.0 * d / 10.0)
    assert new.props().density == pytest.approx(1.04)
    assert d > 0
    assert putty_to_density(m, 0.98, 10.0) < 0


def test_trim_column_centroid_hangs_from_the_top():
    trim = TrimSpec("tail", 140.0, 3.0, -5.0, 3.0, 10.0, "tungsten_putty", 1.0)
    col = trim.column(1.0)
    h = 100.0 / (np.pi * 9.0)
    assert col.mass == 1.0 and col.vol == pytest.approx(100.0)
    assert col.props().cg[2] == pytest.approx(-5.0 - h / 2)
    assert trim.column(0.0).mass == 0.0
    assert trim.part(1.0).mass_g == pytest.approx(1.0)


def test_residual_scaling_and_sides():
    r = Residual("x", "x", 1.05, 1.045, 0.015, 10.0, True)
    assert r.scaled == pytest.approx(1 / 3) and r.ok
    assert not Residual("x", "x", 1.07, 1.045, 0.015, 1.0, True).ok
    one = Residual("r", "r", 5.0, 2.0, 2.0, 1.0, False, side="min")
    assert one.scaled == 0.0 and one.ok
    low = Residual("r", "r", 1.0, 2.0, 2.0, 1.0, False, side="min")
    assert low.scaled == pytest.approx(-0.5) and not low.ok


def test_residuals_cover_targets():
    res = residuals(lure_props(sections(), "mid"), targets())
    ids = [r.id for r in res]
    assert ids == [
        "sink",
        "level",
        "tail_density",
        "head_tail_ratio",
        "self_righting",
    ]
    assert [r.id for r in res if r.hard] == ["sink", "level"]


def test_band_evaluation_sizes_putty_both_ways():
    secs = sections(hw_lo=1.0, hw_hi=3.0)
    trim = TrimSpec("tail", 140.0, 3.0, -5.0, 6.0, 10.0, "tungsten_putty", 3.0)
    out = band_evaluation(secs, targets(), trim)
    lo, hi = out["ends"]["min"], out["ends"]["max"]
    assert lo["trim"]["delta_g"] > 0 and "add" in lo["trim"]["action"]
    assert hi["trim"]["delta_g"] < 0 and "remove" in hi["trim"]["action"]
    # trimmed ends sit at the band-mid density
    assert lo["trim"]["checks"]["density"] == pytest.approx(out["mid_density"], abs=1e-4)
    assert hi["trim"]["checks"]["density"] == pytest.approx(out["mid_density"], abs=1e-4)
    assert out["status"] in {"pass", "fail"}
    assert band_evaluation(secs, targets(), None)["trim"] is None


def test_band_evaluation_clamps_to_capacity():
    secs = sections(hw_lo=0.0, hw_hi=10.0)
    trim = TrimSpec("tail", 140.0, 3.0, -5.0, 1.0, 10.0, "tungsten_putty", 0.5)
    out = band_evaluation(secs, targets(), trim)
    assert out["ends"]["min"]["trim"]["clamped"]
    assert out["ends"]["min"]["trim"]["fill_g"] == pytest.approx(1.0)
    assert out["ends"]["max"]["trim"]["fill_g"] == pytest.approx(0.0)


def test_feasibility_derives_the_head_density():
    # sample-like volumes: head 42.8 cm³, tail 28.2 cm³
    head = SectionInput("head", [Part("body", 42_800.0, (50, 0, 0), 1.0)])
    tail = SectionInput("tail", [Part("body", 28_200.0, (115, 0, 0), 0.75)])
    out = {f["id"]: f for f in feasibility(lure_props([head, tail], "mid"), targets())}
    derived = out["head_density_derived"]
    assert derived["ok"]
    lo, hi = derived["values"]["head_density_range"]
    assert 1.15 < lo < hi < 1.30  # slow sink with a buoyant tail needs a dense head
    ratio = out["ratio_vs_sink"]
    assert ratio["ok"] and lo <= ratio["values"]["head_density"] <= hi
    assert 0.70 <= ratio["values"]["tail_density"] <= 0.80
    # a ratio that pushes the tail out of its band is flagged
    assert not {
        f["id"]: f for f in feasibility(lure_props([head, tail], "mid"), targets(ratio=1.2))
    }["ratio_vs_sink"]["ok"]


def test_old_configs_with_head_density_still_load():
    from luregen.config.models import BuoyancyConfig, SolverConfig

    with pytest.warns(DeprecationWarning):
        b = BuoyancyConfig(sink_bands={"slow_sink": (1.03, 1.06)}, head_density=(1.0, 1.05))
    assert not hasattr(b, "head_density")
    with pytest.warns(DeprecationWarning):
        s = SolverConfig(weights={"head_density": 0.2, "sink": 10.0})
    assert s.weights == {"sink": 10.0}


def test_length_for_weight():
    # density-matched: same mass → same length; 8× the mass → 2× the length
    assert length_for_weight(175.0, 70_000.0, 70.0, 1.0) == pytest.approx(175.0)
    assert length_for_weight(100.0, 50_000.0, 400.0, 1.0) == pytest.approx(200.0)


def test_scale_config_moves_positions(sample_variant):
    from luregen.config import load_config

    cfg = load_config(sample_variant)
    out = scale_config(cfg, 175.0, 140.0)
    assert out.size.solved_length_mm == 140.0
    assert out.hardware[-1].x_mm == pytest.approx(cfg.hardware[-1].x_mm * 0.8)
    assert out.internals.head.keel_top_z_mm == pytest.approx(-6.4)
    assert out.internals.head.wall_thk_mm == cfg.internals.head.wall_thk_mm
    assert out.ballast.x_from_joint_mm == pytest.approx(17.6)
    with pytest.raises(ValueError, match="outside"):
        scale_config(cfg, 175.0, 300.0)


def test_penalty_dead_band_margin_and_override():
    r = Residual("level", "", 0.2, 0.0, 1.0, 30.0, True, deadband=0.25)
    assert r.scaled == pytest.approx(0.2) and r.penalty == 0.0
    r = Residual("level", "", -0.75, 0.0, 1.0, 30.0, True, deadband=0.25)
    assert r.penalty == pytest.approx(-0.5)
    gap = Residual("gap", "", 0.3, 0.0, 1.0, 30.0, True, side="min", margin=0.5)
    assert gap.ok and gap.penalty == pytest.approx(-0.2)
    forced = Residual("gap", "", 0.3, 0.0, 1.0, 30.0, True, side="min", penalty_value=-1.0)
    assert forced.penalty == -1.0


def test_ballast_clearance_is_smooth_between_two_bosses():
    from luregen.physics.balance import PrintBalanceProblem

    p = PrintBalanceProblem.__new__(PrintBalanceProblem)
    p.bosses, p.boss_r, p.targets = [(55.0, -22.0), (86.0, -12.0)], 6.0, targets()
    squeezed = p.clearance(np.array([60.0, 81.0, -14.0]))
    assert squeezed.value < 0 and not squeezed.ok
    # both shortfalls count, so shortening the pocket at either end lowers the penalty
    left = p.clearance(np.array([60.5, 81.0, -14.0]))
    right = p.clearance(np.array([60.0, 80.5, -14.0]))
    assert abs(left.penalty) < abs(squeezed.penalty) and abs(right.penalty) < abs(squeezed.penalty)
    clear = p.clearance(np.array([62.0, 78.0, -14.0]))
    assert clear.ok and clear.penalty == 0.0
