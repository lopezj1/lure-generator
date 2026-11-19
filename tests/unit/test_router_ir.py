import pytest
from pydantic import ValidationError
from router_jobs import JOBS, ROUTER, shapeoko

from luregen.config.loader import load_layered
from luregen.fab.router.ir import Arc, Operation, Program, Setup
from luregen.fab.router.post_config import PostConfig, load_post


@pytest.mark.parametrize("name", sorted(JOBS))
def test_program_json_round_trip(name: str, tmp_path):
    _, _, lib = shapeoko()
    program = JOBS[name](lib)
    again = Program.from_json(program.to_json())
    assert again == program
    program.save(tmp_path / "p.json")
    assert Program.load(tmp_path / "p.json") == program


def test_shapeoko_specs_validate():
    post, machine, lib = shapeoko()
    assert post.word_separator == "" and post.units == "mm"
    assert {"G80", "G81", "G82", "G83", "G43", "G10", "G92"} <= set(post.forbid)
    assert machine.controller.kind == "carbide_motion"
    assert machine.travel.z > 0 and machine.travel.verified
    assert lib.get("ball_3.175").corner == pytest.approx(3.175 / 2)
    assert lib.get("flat_6.35").corner == 0


def test_tool_library_covers_dfm_tools():
    _, _, lib = shapeoko()
    common = load_layered(ROUTER.parents[1] / "designs" / "_common.toml")
    assert set(common["dfm"]["cnc"]["tools"]) <= set(lib.tools)


def test_arc_needs_in_plane_coordinates():
    with pytest.raises(ValidationError, match="needs"):
        Arc(x=1, y=2, cx=0, f=100)
    Arc(z=1, x=2, cz=0, cx=0, plane="zx", f=100)


def test_program_rejects_unknown_tool():
    _, _, lib = shapeoko()
    tool = lib.get("flat_6.35")
    setup = Setup(stock_min=(0, 0, -10), stock_max=(10, 10, 0))
    with pytest.raises(ValidationError, match="unknown tool"):
        Program(
            design="d",
            setup=setup,
            tools={tool.id: tool},
            operations=[Operation(name="x", tool="nope", rpm=1000)],
        )


def test_setup_rejects_safe_z_inside_stock():
    with pytest.raises(ValidationError, match="safe_z"):
        Setup(stock_min=(0, 0, -10), stock_max=(10, 10, 0), safe_z=0)


def test_post_config_rejects_unknown_token():
    with pytest.raises(ValidationError, match="unknown token"):
        PostConfig(header=["(Job {nope})"])


def test_post_toml_round_trips_through_model():
    post = load_post(ROUTER / "post.toml")
    assert PostConfig.model_validate(post.model_dump()) == post
