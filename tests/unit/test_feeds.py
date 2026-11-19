import pytest
from router_jobs import ROUTER

from luregen.fab.router.feeds import (
    IN,
    FeedsError,
    calculate,
    chipload_status,
    compare_presets,
    dial_to_rpm,
    load_feeds,
    preset,
    rpm_to_dial,
    special,
)


@pytest.fixture(scope="module")
def table():
    return load_feeds(ROUTER / "feeds.toml")


def test_calculator_example(table):
    """1/8in 2-flute soft-wood conservative @ 18,000 rpm, as in the calculator UI."""
    f = calculate(table, 3.175, "basswood", "conservative", flutes=2, rpm=18000)
    assert f.feed / IN == pytest.approx(36.0)
    assert f.feed == pytest.approx(914.4)
    assert f.doc == pytest.approx(0.3175)
    assert f.stepover == pytest.approx(1.27)
    assert f.plunge / IN == pytest.approx(12.6)
    assert f.plunge == pytest.approx(320.04)
    assert f.status == "within"


@pytest.mark.parametrize(
    ("dia", "mat", "mode", "feed_ipm", "doc_in", "so_in", "plunge_ipm"),
    [
        (3.175, "soft-wood", "aggressive", 90.0, 0.0625, 0.0875, 31.5),
        (6.35, "pvc_board", "aggressive", 360.0, 0.125, 0.175, 162.0),
        (6.35, "maple", "conservative", 36.0, 0.0125, 0.1, 7.2),
        (1.5875, "hard-wood", "aggressive", 36.0, 0.015625, 0.04375, 7.2),
    ],
)
def test_calculator_rows(table, dia, mat, mode, feed_ipm, doc_in, so_in, plunge_ipm):
    f = calculate(table, dia, mat, mode, flutes=2, rpm=18000)
    assert f.feed / IN == pytest.approx(feed_ipm)
    assert f.doc / IN == pytest.approx(doc_in)
    assert f.stepover / IN == pytest.approx(so_in)
    assert f.plunge / IN == pytest.approx(plunge_ipm)


def test_three_flutes_and_rpm(table):
    f = calculate(table, 3.175, "cedar", "conservative", flutes=3, rpm=10000)
    assert f.feed / IN == pytest.approx(0.001 * 3 * 10000)


def test_vbit_and_surfacing(table):
    v = special(table, "vbit", "basswood", "conservative")
    assert v["feed"] / IN == pytest.approx(40)
    assert v["doc"] / IN == pytest.approx(0.10)
    assert v["plunge"] / IN == pytest.approx(10)
    v = special(table, "vbit", "oak", "aggressive")
    assert (v["feed"] / IN, v["doc"] / IN, v["plunge"] / IN) == pytest.approx((40, 0.15, 15))
    s = special(table, "surfacing", "basswood", "aggressive")
    assert s["feed"] / IN == pytest.approx(100)
    assert s["doc"] / IN == pytest.approx(0.05)
    assert s["plunge"] is None


def test_unknown_material_and_size(table):
    with pytest.raises(FeedsError, match="no feeds material mapping"):
        calculate(table, 3.175, "PETG")
    with pytest.raises(FeedsError, match="no calculator row"):
        calculate(table, 0.794, "basswood")


def test_ball_nose_flagged(table):
    f = calculate(table, 3.175, "basswood", shape="ball")
    assert any("assumption" in n for n in f.notes)


def test_chipload_status():
    assert chipload_status(0.0005, (0.001, 0.0025)) == "below"
    assert chipload_status(0.001, (0.001, 0.0025)) == "within"
    assert chipload_status(0.003, (0.001, 0.0025)) == "above"
    assert chipload_status(None, None) == "n/a"


def test_owner_presets(table):
    rough = preset(table, "owner_rough_flat_3.175", "basswood", rpm=18000)
    assert rough.feed == 4572.0 and rough.plunge == 1016.0
    # 180 IPM / (2 × 18000) = 0.005 in: above the soft-wood range (0.001–0.0025).
    assert rough.chipload_in == pytest.approx(0.005)
    assert rough.status == "above"
    assert rough.doc == 1.0  # owner job: 0.5–1.0 mm main levels (#42)
    assert rough.stepover == pytest.approx(0.7 * 3.175)  # from the calculator, aggressive
    contour = preset(table, "owner_contour_flat_3.175", "basswood")
    assert (contour.feed, contour.doc) == (2540.0, 1.016)
    finish = preset(table, "owner_finish_flat_0.794", "basswood")
    assert finish.stepover == 0.076 and finish.status == "n/a"
    rows = compare_presets(table, "basswood")
    assert {r["preset"] for r in rows} == set(table.presets)


def test_source_cited():
    text = (ROUTER / "feeds.toml").read_text()
    assert "lopezj1/cnc-feeds-speeds-calculator" in text
    assert "A to Z" in text


DIAL_611 = [16500, 18000, 19800, 21500, 24200, 27000]


@pytest.mark.parametrize(
    ("dial", "rpm"), [(1, 16500), (3, 19800), (3.5, 20650), (6, 27000), (0.5, 16500)]
)
def test_dial_to_rpm_interpolates_marks(dial, rpm):
    assert dial_to_rpm(dial, DIAL_611) == pytest.approx(rpm)


@pytest.mark.parametrize(
    ("rpm", "dial"), [(18000, 2), (20650, 3.5), (24000, 5), (17000, 1.25), (30000, 6)]
)
def test_rpm_to_dial_rounds_to_quarter(rpm, dial):
    assert rpm_to_dial(rpm, DIAL_611) == dial


def test_default_rpm_follows_chart_dial(table):
    # Chart: hardwood (Mahogany row) dial 3, pine dial 3.5, PVC dial 1.
    assert table.default_rpm("oak", DIAL_611) == pytest.approx(19800)
    assert table.default_rpm("basswood", DIAL_611) == pytest.approx(20650)
    assert table.default_rpm("pvc_board", DIAL_611) == pytest.approx(16500)
    assert table.default_rpm("PETG", DIAL_611) == table.rpm.default
    assert table.default_rpm("oak", []) == table.rpm.default
