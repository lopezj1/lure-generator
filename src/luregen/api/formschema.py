"""Form schema for the config editor (ported from spike #57).

`postprocess_schema(LureConfig.model_json_schema(), materials)` turns pydantic's schema into a
draft-07 schema that @sjsf/form renders well. The client reads only the `x-*` hints:

- R1 inline `$defs` (LureConfig has no recursion); `$ref` siblings (default, description) are
  merged into the inlined copy.
- R2 `anyOf: [T, {type: null}]` → T plus `x-nullable: true`; a `default: null` is dropped.
  Null in data means "key absent" (TOML has no null; pydantic fills the default).
- R2b whole-object `default`s are dropped (the children carry their own; they contain nulls).
- R3 2-number `prefixItems` → draft-07 tuple `items: [num, num]`, `additionalItems: false`,
  `x-widget: "range-pair"`. The data stays `[lo, hi]`.
- R4 `dict[Literal[...], V]` → fixed `properties` (none required), `additionalProperties: false`.
- R5 number/integer with both bounds → `x-widget: "slider"` + `x-step`.
- R6 `format: "path"` → `x-widget: "path"`.
- R7 material fields → `x-widget: "material-select"` + `x-options` from spec/materials.toml,
  filtered by category per field.
- R8 fields `resolve()` fills → `x-derived: "catalog"` (overridable) or `x-derived: "computed"`
  + `readOnly: true`.
- R9 readable titles: "Length Mm" → "Length (mm)", class names ("SizeConfig") → key name;
  `x-label` ("Length") and `x-unit` ("mm") for forms that show the unit as an input suffix.
- R10 editor groups (#121): every node gets `x-group` (a tab id from `GROUPS`) and `x-level`
  (`basic` or `advanced`) from `FIELD_GROUPS`; the root lists the tabs in `x-groups`. A path
  without its own entry takes its nearest mapped ancestor's group and is `advanced`, so new
  config fields show up (in an expert section) without touching this table. An object is
  `basic` when any of its children is.
- R12 `hardware[].id` gets `x-suggest` ([{value, label}] from spec/hardware) for a datalist.
- R11 layout hints: `x-layout: "table"` for arrays of objects and R4 fixed-key maps (rows ×
  columns), `"keyvalue"` for free-key maps, `"list"` for arrays of strings; `x-rows` on an
  object whose child objects render as the rows of one table (`ROW_TABLES`).

`restore(data)` is the reverse pass for form output (drops `None`). `provenance(variant)` maps
JSON pointers to the layer of the `extends` chain that last set them.
"""

import copy
import math
import tomllib
from pathlib import Path
from typing import Any

# R7: material categories each material field may pick from.
MATERIAL_FIELDS: dict[tuple[str, ...], list[str]] = {
    ("ballast", "material"): ["ballast"],
    ("process", "material"): ["plastic", "resin", "wood", "foam_board"],
    ("hardware", "*", "material"): ["metal"],
}
# R8: fields `resolve()` fills. "computed" is never user-set; "catalog" may be overridden.
DERIVED: dict[tuple[str, ...], str] = {
    ("buoyancy", "water_density"): "computed",
    ("ballast", "density"): "catalog",
    ("process", "material_density"): "catalog",
    ("hardware", "*", "name"): "catalog",
    ("hardware", "*", "mass_g"): "catalog",
    ("hardware", "*", "material"): "catalog",
    ("hardware", "*", "density"): "catalog",
    ("hardware", "*", "measured"): "catalog",
}
UNITS = {"mm": "mm", "deg": "°", "oz": "oz", "g": "g", "frac": "fraction", "mm2": "mm²"}

# R10: the editor's tabs, in order. The browser renders whatever this lists.
GROUPS: list[dict[str, str]] = [
    {"id": "shape", "title": "Shape & size", "description": "Length, profile, joint, material"},
    {"id": "weight", "title": "Weight & balance", "description": "Sink rate, ballast, internals"},
    {"id": "hardware", "title": "Hardware", "description": "Eyes, hooks, rings, pins"},
    {"id": "print", "title": "Print", "description": "Filament slots, supports, print checks"},
    {"id": "cnc", "title": "CNC", "description": "Stock, pockets, tabs, CAM operations"},
    {"id": "advanced", "title": "Advanced", "description": "Solver, report, identity"},
]
B, A = "basic", "advanced"
# R10: path pattern → (group, level). Exact entries set the level; descendants without an
# entry inherit the group as `advanced`. Keep basic sets small: what a user tunes per lure.
FIELD_GROUPS: dict[tuple[str, ...], tuple[str, str]] = {
    ("name",): ("advanced", A),
    ("family",): ("advanced", A),
    ("size",): ("shape", A),
    ("size", "length_mm"): ("shape", B),
    ("size", "target_weight_oz"): ("shape", B),
    ("profile",): ("shape", A),
    ("profile", "source"): ("shape", B),
    ("profile", "image"): ("shape", B),
    ("profile", "preset"): ("shape", B),
    ("profile", "scale"): ("shape", B),
    ("profile", "section_shape"): ("shape", B),
    ("profile", "width_rule", "value"): ("shape", B),
    ("joint",): ("shape", A),
    ("joint", "joint_frac"): ("shape", B),
    ("process",): ("shape", A),
    ("process", "method"): ("shape", B),
    ("process", "body_style"): ("shape", B),
    ("process", "material"): ("shape", B),
    ("process", "rigging"): ("shape", B),
    ("buoyancy",): ("weight", A),
    ("buoyancy", "water"): ("weight", B),
    ("buoyancy", "target_sink"): ("weight", B),
    ("buoyancy", "trim_pocket"): ("weight", B),
    ("ballast",): ("weight", A),
    ("ballast", "material"): ("weight", B),
    ("ballast", "mass_g"): ("weight", B),
    ("internals",): ("weight", A),
    ("hardware",): ("hardware", B),
    ("hardware", "*", "id"): ("hardware", B),
    ("hardware", "*", "name"): ("hardware", B),
    ("hardware", "*", "section"): ("hardware", B),
    ("hardware", "*", "x_mm"): ("hardware", B),
    ("hardware", "*", "z_mm"): ("hardware", B),
    ("hardware", "*", "qty"): ("hardware", B),
    ("print",): ("print", A),
    ("print", "body_slot"): ("print", B),
    ("print", "interface_slot"): ("print", B),
    ("print", "supports"): ("print", B),
    ("print", "pause"): ("print", B),
    ("dfm",): ("advanced", A),
    ("dfm", "print"): ("print", A),
    ("dfm", "cnc"): ("cnc", A),
    ("cnc",): ("cnc", A),
    ("cnc", "stock", "thickness_mm"): ("cnc", B),
    ("cnc", "stock", "margin_mm"): ("cnc", B),
    ("cnc", "air_pockets"): ("cnc", B),
    ("cnc", "air_pockets", "*"): ("cnc", B),
    ("cnc", "air_pockets", "*", "section"): ("cnc", B),
    ("cnc", "air_pockets", "*", "x_mm"): ("cnc", B),
    ("cnc", "air_pockets", "*", "z_mm"): ("cnc", B),
    ("cnc", "air_pockets", "*", "length_mm"): ("cnc", B),
    ("cnc", "air_pockets", "*", "height_mm"): ("cnc", B),
    ("cnc", "air_pockets", "*", "depth_mm"): ("cnc", B),
    ("cnc", "tabs", "count"): ("cnc", B),
    ("cnc", "registration", "enabled"): ("cnc", B),
    ("report",): ("advanced", A),
    ("solver",): ("advanced", A),
}
# R10: friendlier form labels than the key names give (the title keeps the key's wording).
LABELS: dict[tuple[str, ...], str] = {
    ("size", "target_weight_oz"): "Target weight",
    ("profile", "scale"): "Photo scale",
    ("profile", "width_rule", "value"): "Width ÷ depth",
    ("joint", "joint_frac"): "Joint position (of length)",
    ("ballast", "mass_g"): "Ballast mass (pin)",
    ("buoyancy", "cb_above_cg_frac"): "CB above CG (fraction)",
    ("hardware", "*", "id"): "Catalog id",
    ("internals", "head"): "Head",
    ("internals", "tail"): "Tail",
    ("report", "show_oz"): "Show ounces",
}
# R11: objects whose child objects are the rows of one table (None: all of them).
ROW_TABLES: dict[tuple[str, ...], list[str] | None] = {
    ("internals",): ["head", "tail"],
    ("cnc", "ops"): None,
}
UI_SCHEMA: dict[str, Any] = {
    "hardware": {"ui:options": {"orderable": True, "addable": True, "removable": True}},
    "dfm": {"cnc": {"tools": {"ui:options": {"orderable": True}}}},
}


def _deref(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_deref(n, defs) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        return _deref({**target, **siblings}, defs)
    return {k: _deref(v, defs) for k, v in node.items()}


def _is_null(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "null"


def _nice_step(lo: float, hi: float, integer: bool) -> float:
    if integer or hi <= lo:
        return 1
    return 10 ** math.floor(math.log10((hi - lo) / 100))


def _label_unit(key: str) -> tuple[str, str | None]:
    words = key.split("_")
    unit = None
    if len(words) > 1 and words[-1] in UNITS:
        unit = UNITS[words.pop()]
    text = " ".join(words).replace("x dia", "× dia")
    return text[:1].upper() + text[1:], unit


def _title(key: str) -> str:
    text, unit = _label_unit(key)
    return f"{text} ({unit})" if unit else text


def _group(path: tuple[str, ...]) -> tuple[str, str]:
    """R10: (group, level) of `path`: its own entry, else the nearest ancestor's group."""
    if path in FIELD_GROUPS:
        return FIELD_GROUPS[path]
    for n in range(len(path) - 1, 0, -1):
        if path[:n] in FIELD_GROUPS:
            return FIELD_GROUPS[path[:n]][0], A
    return "advanced", A


def _match(table: dict[tuple[str, ...], Any], path: tuple[str, ...]) -> Any:
    for pattern, value in table.items():
        if len(pattern) == len(path) and all(
            p in ("*", q) for p, q in zip(pattern, path, strict=True)
        ):
            return value
    return None


def _walk(node: dict[str, Any], path: tuple[str, ...], ctx: dict[str, Any]) -> dict[str, Any]:
    node = dict(node)

    if "anyOf" in node:  # R2
        options = [o for o in node["anyOf"] if not _is_null(o)]
        if len(options) == 1 and len(options) < len(node["anyOf"]):
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            node = {**options[0], **rest, "x-nullable": True}
            if node.get("default", 0) is None:
                del node["default"]

    if path and path[-1] == "*" and node.get("title") in ctx["def_names"] and len(path) > 1:
        node["title"] = _title(path[-2]).rstrip("s")
    if (  # R9
        path
        and path[-1] != "*"
        and (
            "title" not in node
            or node["title"] in ctx["def_names"]
            or node["title"] == path[-1].replace("_", " ").title()
        )
    ):
        node["title"] = _title(path[-1])
    if path and path[-1] != "*" and not path[-1].isdigit():
        label, unit = _label_unit(path[-1])
        if node.get("type") not in ("number", "integer", "array") or unit == "fraction":
            label, unit = _label_unit(path[-1] + "_")  # keep the last word in the label
            label = label.rstrip()
        if node["title"] == _title(path[-1]):
            node["x-label"] = _match(LABELS, path) or label
        if unit:
            node["x-unit"] = unit

    kind = node.get("type")
    if kind == "object":
        node.pop("default", None)  # R2b
        names = node.get("propertyNames", {})
        if "enum" in names and isinstance(node.get("additionalProperties"), dict):  # R4
            value_schema = node.pop("additionalProperties")
            node.pop("propertyNames")
            node["properties"] = {k: copy.deepcopy(value_schema) for k in names["enum"]}
            node["additionalProperties"] = False
            node["x-layout"] = "table"  # R11
        elif isinstance(node.get("additionalProperties"), dict) and not node.get("properties"):
            node["x-layout"] = "keyvalue"  # R11
            node["additionalProperties"] = _walk(node["additionalProperties"], (*path, "*"), ctx)
        node["properties"] = {
            k: _walk(v, (*path, k), ctx) for k, v in node.get("properties", {}).items()
        }
        if path in ROW_TABLES:  # R11
            rows = ROW_TABLES[path]
            node["x-rows"] = rows if rows is not None else list(node["properties"])
    elif kind == "array":
        prefix = node.pop("prefixItems", None)
        if prefix is not None:  # R3
            node["items"] = [_walk(p, (*path, str(i)), ctx) for i, p in enumerate(prefix)]
            for i, item in enumerate(node["items"]):
                item["title"] = ["min", "max"][i] if len(prefix) == 2 else str(i)
            node["additionalItems"] = False
            if len(prefix) == 2 and all(p.get("type") in ("number", "integer") for p in prefix):
                node["x-widget"] = "range-pair"
        elif isinstance(node.get("items"), dict):
            node["items"] = _walk(node["items"], (*path, "*"), ctx)
            item_type = node["items"].get("type")
            if item_type == "object":
                node["x-layout"] = "table"  # R11
            elif item_type == "string":
                node["x-layout"] = "list"
    elif kind in ("number", "integer"):
        lo, hi = node.get("minimum"), node.get("maximum")
        if lo is not None and hi is not None:  # R5
            node["x-widget"] = "slider"
            node["x-step"] = _nice_step(lo, hi, kind == "integer")
    elif kind == "string":
        if node.get("format") == "path":  # R6
            del node["format"]
            node["x-widget"] = "path"
        categories = _match(MATERIAL_FIELDS, path)
        if categories is not None:  # R7
            node["x-widget"] = "material-select"
            node["x-options"] = [
                n for n, m in ctx["materials"].items() if m.get("category") in categories
            ]

    if path == ("hardware", "*", "id") and ctx.get("hardware"):  # R12
        node["x-suggest"] = [
            {"value": k, "label": str(v.get("name", k))} for k, v in ctx["hardware"].items()
        ]

    derived = _match(DERIVED, path)
    if derived:  # R8
        node["x-derived"] = derived
        if derived == "computed":
            node["readOnly"] = True

    group, level = _group(path)  # R10
    if kind == "object" and any(c.get("x-level") == B for c in node.get("properties", {}).values()):
        level = B
    if kind == "object" and node.get("x-layout") != "table" and node.get("properties"):
        # A container renders in every tab one of its descendants belongs to.
        node["x-groups-in"] = sorted(
            {g for c in node["properties"].values() for g in c.get("x-groups-in", [c["x-group"]])}
        )
    node["x-group"] = group
    node["x-level"] = level
    return node


def postprocess_schema(
    schema: dict[str, Any],
    materials: dict[str, dict[str, Any]],
    hardware: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return `(form schema, ui schema)`; widget choice lives in the schema's `x-*` hints."""
    defs = schema.get("$defs", {})
    root = _deref({k: v for k, v in schema.items() if k != "$defs"}, defs)  # R1
    ctx = {"materials": materials, "def_names": set(defs), "hardware": hardware or {}}
    out = _walk(root, (), ctx)
    out["$schema"] = "http://json-schema.org/draft-07/schema#"
    out["x-groups"] = copy.deepcopy(GROUPS)
    return out, copy.deepcopy(UI_SCHEMA)


def restore(data: Any) -> Any:
    """Reverse pass for form output: drop None (means unset). Tuples and dicts already match."""
    if isinstance(data, dict):
        return {k: restore(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [restore(v) for v in data]
    return data


def extends_chain(variant: Path) -> list[Path]:
    """The variant and its `extends` ancestors, child first (as `load_layered` walks them)."""
    chain: list[Path] = []
    current: Path | None = variant.resolve()
    while current is not None and current not in chain and current.is_file():
        chain.append(current)
        parent = tomllib.loads(current.read_text()).get("extends")
        current = (current.parent / parent).resolve() if parent else None
    return chain


def provenance(chain: list[Path]) -> dict[str, int]:
    """Which layer (index into `chain`, child first) last set each leaf, by JSON pointer.

    Scalars and lists replace whole (like `deep_merge`); arrays of tables are also tracked
    per item and per key.
    """
    out: dict[str, int] = {}

    def mark(data: dict[str, Any], prefix: str, layer: int) -> None:
        for key, value in data.items():
            if key == "extends":
                continue
            pointer = f"{prefix}/{key}"
            if isinstance(value, dict):
                mark(value, pointer, layer)
                continue
            for stale in [p for p in out if p == pointer or p.startswith(pointer + "/")]:
                del out[stale]
            out[pointer] = layer
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
                for i, item in enumerate(value):
                    mark(item, f"{pointer}/{i}", layer)

    for layer in reversed(range(len(chain))):
        mark(tomllib.loads(chain[layer].read_text()), "", layer)
    return out
