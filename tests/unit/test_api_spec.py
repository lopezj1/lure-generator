"""W1 (#62): spec catalogs, variant read/validate/save (tomlkit diff writer), form schema."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the `web` dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from luregen.api.app import create_app  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
VARIANT_URL = "/api/variants/glide/striper_175_petg"
PRESET = "spec/designs/glide/profiles/presets/placeholder_175.csv"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "spec", tmp_path / "spec")
    return tmp_path


@pytest.fixture
def client(root: Path) -> TestClient:
    return TestClient(create_app(root))


@pytest.fixture
def variant_file(root: Path) -> Path:
    return root / "spec/designs/glide/variants/striper_175_petg.toml"


def _load(client: TestClient) -> dict:
    res = client.get(VARIANT_URL)
    assert res.status_code == 200, res.text
    return res.json()


def _save(client: TestClient, config: dict, etag: str | None, **kw):
    return client.put(VARIANT_URL, json={"config": config, "etag": etag}, **kw)


# --- catalogs ------------------------------------------------------------------------------


def test_designs_and_variants(client):
    designs = client.get("/api/designs").json()
    assert designs == [
        {
            "family": "glide",
            "path": "spec/designs/glide/family.toml",
            "variants": ["glide_2oz_tpu_fin", "striper_175_petg"],
        }
    ]
    variants = client.get("/api/designs/glide/variants").json()
    assert variants == [
        {
            "name": "glide_2oz_tpu_fin",
            "path": "spec/designs/glide/variants/glide_2oz_tpu_fin.toml",
            "method": "print",
            "material": "PETG",
        },
        {
            "name": "striper_175_petg",
            "path": "spec/designs/glide/variants/striper_175_petg.toml",
            "method": "print",
            "material": "PETG",
        },
    ]
    assert client.get("/api/designs/nope/variants").status_code == 404


@pytest.mark.parametrize("bad", [".hidden", "a%2Fb", "%2E%2E", "a b"])
def test_path_params_are_single_safe_segments(client, bad):
    assert client.get(f"/api/designs/{bad}/variants").status_code in (404, 422)
    assert client.get(f"/api/variants/glide/{bad}").status_code in (404, 422)


def test_presets(client):
    assert client.get("/api/designs/glide/presets").json() == [PRESET]
    assert client.get("/api/designs/nope/presets").status_code == 404


def test_machines_and_materials(client):
    machines = {m["id"]: m for m in client.get("/api/machines").json()}
    assert machines["snapmaker_u1"]["type"] == "printer"
    assert machines["shapeoko3_xxl"]["type"] == "router"
    assert machines["shapeoko3_xxl"]["path"] == "spec/routers/shapeoko3_xxl"
    materials = {m["name"]: m for m in client.get("/api/materials").json()}
    assert materials["PETG"] == {
        "name": "PETG",
        "density": 1.27,
        "category": "plastic",
        "range": None,
    }
    assert materials["basswood"]["range"] == [0.37, 0.47]


# --- variant read ------------------------------------------------------------------------


def test_get_variant(client, root):
    res = client.get(VARIANT_URL)
    body = res.json()
    assert res.headers["etag"] == f'"{body["etag"]}"'
    assert str(root) not in res.text and str(Path.home()) not in res.text
    assert body["own"]["size"] == {"length_mm": 175}
    assert "hardware" in body["own"] and "joint" not in body["own"]
    assert body["layered"]["profile"]["preset"] == PRESET
    assert body["resolved"]["profile"]["preset"] == PRESET
    assert body["resolved"]["process"]["material_density"] == 1.27
    assert "image" not in body["resolved"]["profile"]  # None dropped (restore)
    assert body["errors"] == []
    labels = [layer["label"] for layer in body["layers"]]
    assert labels == ["variant", "family", "common"]
    prov = body["provenance"]
    assert labels[prov["/size/length_mm"]] == "variant"
    assert labels[prov["/joint/gap_mm"]] == "family"
    assert labels[prov["/buoyancy/water"]] == "common"
    assert labels[prov["/hardware/0/x_mm"]] == "variant"
    inherited = body["inherited"]  # the parents alone: what "reset to inherited" restores
    assert inherited["joint"]["gap_mm"] == 2.3
    assert inherited["profile"]["preset"] == PRESET
    assert "size" not in inherited and "hardware" not in inherited


def test_get_invalid_variant_reports_errors(client, variant_file):
    variant_file.write_text(variant_file.read_text().replace("length_mm = 175", "length_mm = 5"))
    body = _load(client)
    assert body["resolved"] is None
    assert body["errors"][0]["path"] == ["size", "length_mm"]


# --- schema and validate -----------------------------------------------------------------


def test_form_schema(client):
    body = client.get("/api/schema/lure-config").json()
    schema, ui = body["schema"], body["uiSchema"]
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert "$defs" not in json.dumps(schema)
    props = schema["properties"]
    length = props["size"]["properties"]["length_mm"]
    assert length["x-widget"] == "slider" and length["x-nullable"] is True
    assert length["title"] == "Length (mm)"
    material = props["process"]["properties"]["material"]
    assert material["x-widget"] == "material-select"
    assert "PETG" in material["x-options"] and "tungsten" not in material["x-options"]
    assert props["buoyancy"]["properties"]["water_density"]["readOnly"] is True
    mass = props["hardware"]["items"]["properties"]["mass_g"]
    assert mass["x-widget"] == "range-pair" and mass["x-derived"] == "catalog"
    assert set(props["buoyancy"]["properties"]["sink_bands"]["properties"]) == {
        "float",
        "suspend",
        "slow_sink",
    }
    assert ui["hardware"]["ui:options"]["orderable"] is True


def test_form_schema_groups(client):
    """#121: every node carries an editor group and level; tables get layout hints."""
    schema = client.get("/api/schema/lure-config").json()["schema"]
    ids = [g["id"] for g in schema["x-groups"]]
    assert ids == ["shape", "weight", "hardware", "print", "cnc", "advanced"]
    props = schema["properties"]

    def leaves(node, path=()):
        if node.get("type") == "object" and node.get("properties"):
            for k, v in node["properties"].items():
                yield from leaves(v, (*path, k))
        else:
            yield path, node

    for path, node in leaves(schema):
        assert node["x-group"] in ids, path
        assert node["x-level"] in ("basic", "advanced"), path

    length = props["size"]["properties"]["length_mm"]
    assert (length["x-group"], length["x-level"]) == ("shape", "basic")
    assert (length["x-label"], length["x-unit"]) == ("Length", "mm")
    assert props["size"]["x-level"] == "basic"  # a child is basic
    smoothing = props["profile"]["properties"]["smoothing"]
    assert (smoothing["x-group"], smoothing["x-level"]) == ("shape", "advanced")
    assert props["solver"]["x-group"] == "advanced"
    # dfm spans two tabs: its children go to Print and CNC.
    assert props["dfm"]["x-groups-in"] == ["cnc", "print"]
    assert props["dfm"]["properties"]["print"]["x-group"] == "print"

    hardware = props["hardware"]
    assert hardware["x-layout"] == "table" and hardware["x-group"] == "hardware"
    cols = hardware["items"]["properties"]
    assert cols["x_mm"]["x-level"] == "basic" and cols["density"]["x-level"] == "advanced"
    assert {"value": "treble_2", "label": "Treble hook #2"} in cols["id"]["x-suggest"]
    assert props["buoyancy"]["properties"]["sink_bands"]["x-layout"] == "table"
    assert props["print"]["properties"]["overrides"]["x-layout"] == "keyvalue"
    assert props["dfm"]["properties"]["cnc"]["properties"]["tools"]["x-layout"] == "list"
    assert props["internals"]["x-rows"] == ["head", "tail"]
    assert "finish3d" in props["cnc"]["properties"]["ops"]["x-rows"]


def test_form_schema_unmapped_fields_default_to_advanced():
    """A field missing from FIELD_GROUPS takes its parent's group as advanced (#121)."""
    from luregen.api.formschema import postprocess_schema

    schema = {
        "type": "object",
        "properties": {
            "size": {
                "type": "object",
                "properties": {
                    "length_mm": {"type": "number"},
                    "new_knob_mm": {"type": "number"},
                },
            },
            "brand_new": {"type": "object", "properties": {"x": {"type": "number"}}},
        },
    }
    out, _ = postprocess_schema(schema, {})
    size = out["properties"]["size"]["properties"]
    assert (size["length_mm"]["x-group"], size["length_mm"]["x-level"]) == ("shape", "basic")
    assert (size["new_knob_mm"]["x-group"], size["new_knob_mm"]["x-level"]) == (
        "shape",
        "advanced",
    )
    new = out["properties"]["brand_new"]
    assert (new["x-group"], new["x-level"]) == ("advanced", "advanced")
    assert new["properties"]["x"]["x-group"] == "advanced"


def test_validate(client):
    config = _load(client)["resolved"]
    url = f"{VARIANT_URL}/validate"
    assert client.post(url, json=config).json() == []

    both = {**config, "size": {"length_mm": 175, "target_weight_oz": 2.0}}
    errors = client.post(url, json=both).json()
    assert errors == [
        {"path": ["size"], "message": "set exactly one of size.length_mm and size.target_weight_oz"}
    ]

    unknown = {**config, "process": {**config["process"], "material": "unobtainium"}}
    unknown["process"].pop("material_density")
    errors = client.post(url, json=unknown).json()
    assert errors[0]["path"] == [] and "unobtainium" in errors[0]["message"]

    outside = {**config, "profile": {**config["profile"], "preset": "../elsewhere.csv"}}
    errors = client.post(url, json=outside).json()
    assert errors[0]["path"] == ["profile", "preset"]

    nulls = {**config, "joint": {**config["joint"], "line_tie_z": None}}
    assert client.post(url, json=nulls).json() == []  # None means unset


# --- save (tomlkit diff writer) ----------------------------------------------------------


def test_noop_save_keeps_file_identical(client, variant_file):
    before = variant_file.read_bytes()
    body = _load(client)
    res = _save(client, body["resolved"], body["etag"])
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is False and res.json()["backup"] is None
    assert variant_file.read_bytes() == before
    assert not variant_file.with_name(variant_file.name + ".bak").exists()
    # The unresolved merged dict round-trips too.
    assert _save(client, body["layered"], body["etag"]).json()["changed"] is False


def test_save_edits_in_place_and_keeps_comments(client, variant_file):
    before = variant_file.read_text()
    body = _load(client)
    config = body["resolved"]  # full form output: defaults and catalog values included
    config["size"]["length_mm"] = 180.0
    config["hardware"][1]["z_mm"] = -21.5
    res = client.put(VARIANT_URL, json={"config": config}, headers={"If-Match": body["etag"]})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["changed"] is True
    assert out["backup"] == "spec/designs/glide/variants/striper_175_petg.toml.bak"
    assert out["variant"]["etag"] != body["etag"]
    assert out["variant"]["resolved"]["size"]["length_mm"] == 180

    after = variant_file.read_text()
    expected = before.replace("length_mm = 175", "length_mm = 180").replace(
        "x_mm = 55\nz_mm = -22", "x_mm = 55\nz_mm = -21.5"
    )
    assert after == expected  # only those two lines; comments and layout kept
    assert variant_file.with_name(variant_file.name + ".bak").read_text() == before


def test_override_then_reset_removes_key(client, variant_file):
    before = variant_file.read_text()
    body = _load(client)
    config = body["resolved"]
    config["joint"]["gap_mm"] = 3.0
    config["profile"]["width_rule"]["value"] = 0.4
    config["report"] = {"show_oz": False}
    res = _save(client, config, body["etag"])
    assert res.status_code == 200, res.text
    text = variant_file.read_text()
    assert "[joint]\ngap_mm = 3\n" in text
    assert "[profile.width_rule]\nvalue = 0.4\n" in text
    assert "[report]\nshow_oz = false\n" in text
    assert text.count("=") == before.count("=") + 3  # nothing else was baked in

    body = res.json()["variant"]
    assert body["provenance"]["/joint/gap_mm"] == 0  # now set by the variant
    config = body["resolved"]
    config["joint"]["gap_mm"] = 2.3  # back to the family value
    config["profile"]["width_rule"]["value"] = 0.34
    config["report"]["show_oz"] = True
    res = _save(client, config, body["etag"])
    assert res.status_code == 200, res.text
    assert variant_file.read_text() == before


def test_save_adds_key_at_end_of_table_and_relative_paths(client, root, variant_file):
    body = _load(client)
    config = body["resolved"]
    config["process"]["material_density"] = 1.3  # catalog override
    config["profile"]["preset"] = "uploads/abc.csv"
    res = _save(client, config, body["etag"])
    assert res.status_code == 200, res.text
    text = variant_file.read_text()
    assert 'rigging = "screw_eye"\nmaterial_density = 1.3\n\n# Positions' in text
    assert '[profile]\npreset = "../../../../uploads/abc.csv"\n' in text
    out = res.json()["variant"]
    assert out["resolved"]["profile"]["preset"] == "uploads/abc.csv"
    assert str(root) not in res.text


def test_save_preconditions_and_errors(client, variant_file):
    body = _load(client)
    config = body["resolved"]
    assert _save(client, config, None).status_code == 428
    stale = _save(client, config, "0" * 16)
    assert stale.status_code == 409
    assert stale.json()["detail"]["etag"] == body["etag"]

    bad = {**config, "size": {"length_mm": 5}}
    res = _save(client, bad, body["etag"])
    assert res.status_code == 422
    assert res.json()["detail"][0]["path"] == ["size", "length_mm"]

    # A key inherited from the family can't be removed by the variant.
    unset = {k: v for k, v in config.items() if k != "joint"}
    res = _save(client, unset, body["etag"])
    assert res.status_code == 422
    assert "inherited" in res.json()["detail"][0]["message"]
    assert not variant_file.with_name(variant_file.name + ".bak").exists()
    assert not list(variant_file.parent.glob(".*"))  # no temp files left


def test_save_never_touches_extends_or_name(client, variant_file):
    body = _load(client)
    config = {**body["resolved"], "name": "renamed", "extends": "../../elsewhere.toml"}
    config["size"]["length_mm"] = 190
    res = _save(client, config, body["etag"])
    assert res.status_code == 200, res.text
    text = variant_file.read_text()
    assert 'extends = "../family.toml"\nname = "striper_175_petg"\n' in text


# --- spec status --------------------------------------------------------------------------


def test_spec_status_without_git(client):
    assert client.get("/api/spec/status").json() == {"git": False, "dirty": False, "files": []}


def test_spec_status_with_git(root, client, variant_file):
    if shutil.which("git") is None:
        pytest.skip("git not installed")

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", "spec")
    git("commit", "-qm", "spec")
    assert client.get("/api/spec/status").json() == {"git": True, "dirty": False, "files": []}
    variant_file.write_text(variant_file.read_text() + "\n")
    (root / "spec" / "designs" / "glide" / "variants" / "new.toml").write_text("")
    status = client.get("/api/spec/status").json()
    assert status["dirty"] is True
    assert {f["path"]: f["status"] for f in status["files"]} == {
        "spec/designs/glide/variants/striper_175_petg.toml": " M",
        "spec/designs/glide/variants/new.toml": "??",
    }


def test_every_field_has_a_concise_description():
    """Every editor label has a hover tooltip: each field and object carries a one-line
    description (#132). Fixed-key map rows and [min, max] items are labeled by their key."""
    from luregen.api.formschema import postprocess_schema
    from luregen.config.models import LureConfig

    out, _ = postprocess_schema(LureConfig.model_json_schema(), {})
    missing, long = [], []

    def visit(node, path):
        for k, c in node.get("properties", {}).items():
            p = f"{path}.{k}" if path else k
            if node.get("x-layout") != "table":  # R4 rows are self-describing keys
                d = c.get("description", "")
                if not d:
                    missing.append(p)
                elif len(d) > 110 or "\n" in d:
                    long.append(p)
            visit(c, p)
        if isinstance(node.get("items"), dict):
            visit(node["items"], f"{path}[]")

    visit(out, "")
    assert not missing, f"fields without a description: {missing}"
    assert not long, f"descriptions over one short line: {long}"
