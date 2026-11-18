"""Spike #57: turn `LureConfig.model_json_schema()` into a schema @sjsf/form renders well.

Rules (all server side; the client only reads `x-*` hints):
  R1  inline `$defs` (LureConfig has no recursion); `$ref` siblings (default, description)
      are merged into the inlined copy, so draft-07 "siblings of $ref are ignored" can't bite.
  R2  `anyOf: [T, {type: null}]` -> T, plus `x-nullable: true`; a `default: null` is dropped.
      Null in data == key absent (TOML has no null, pydantic fills the default).
  R3  2-number `prefixItems` tuple -> draft-07 tuple `items: [num, num]`, `additionalItems: false`,
      `x-widget: "range-pair"`. Data shape stays `[lo, hi]`, so nothing to reverse.
  R4  `dict[Literal[...], V]` (propertyNames.enum + additionalProperties) -> fixed `properties`
      (none required), `additionalProperties: false`.
  R5  number/integer with both bounds -> `x-widget: "slider"` + `x-step`.
  R6  `format: "path"` -> dropped (Ajv doesn't know it) and replaced by `x-widget: "path"`.
  R7  string fields named `material` -> `x-widget: "material-select"` + `x-options`
      (names from spec/materials.toml, filtered by category per field).
  R8  fields filled by `resolve()` -> `x-derived: "catalog"` (overridable, shown with the
      resolved value as placeholder) or `readOnly: true` + `x-derived: "computed"` (water_density).
  R9  titles: pydantic's "Length Mm" -> "Length (mm)", class names ("SizeConfig") -> key.
  R2b whole-object `default`s are dropped (children carry their defaults; they contain nulls).

`postprocess_schema(schema, materials)` returns `(schema, ui_schema)`; `restore(data)` is the
reverse pass for form output (drops `None`s; everything else already matches pydantic).
"""

from __future__ import annotations

import copy
import json
import math
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

# R7: which material categories each material field may pick from.
MATERIAL_FIELDS: dict[tuple[str, ...], list[str]] = {
    ("ballast", "material"): ["ballast"],
    ("process", "material"): ["plastic", "resin", "wood", "foam_board"],
    ("hardware", "*", "material"): ["metal"],
}
# R8: fields `resolve()` fills. "computed" = never user-set; "catalog" = override allowed.
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
UNITS = {"mm": "mm", "deg": "°", "oz": "oz", "g": "g", "frac": "fraction"}


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


def _is_null(s: Any) -> bool:
    return isinstance(s, dict) and s.get("type") == "null"


def _nice_step(lo: float, hi: float, integer: bool) -> float:
    if integer:
        return 1
    return 10 ** math.floor(math.log10((hi - lo) / 100))


def _title(key: str) -> str:
    words = key.split("_")
    unit = None
    if len(words) > 1 and words[-1] in UNITS:
        unit = UNITS[words.pop()]
    text = " ".join(words).replace("x dia", "× dia")
    text = text[:1].upper() + text[1:]
    return f"{text} ({unit})" if unit else text


def _match(table: dict[tuple[str, ...], Any], path: tuple[str, ...]) -> Any:
    for pattern, value in table.items():
        if len(pattern) == len(path) and all(
            p in ("*", q) for p, q in zip(pattern, path, strict=True)
        ):
            return value
    return None


def _walk(node: dict[str, Any], path: tuple[str, ...], ctx: dict[str, Any]) -> dict[str, Any]:
    node = dict(node)

    # R2
    if "anyOf" in node:
        options = [o for o in node["anyOf"] if not _is_null(o)]
        if len(options) == 1 and len(options) < len(node["anyOf"]):
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            node = {**options[0], **rest, "x-nullable": True}
            if node.get("default", 0) is None:
                del node["default"]

    if path and path[-1] == "*" and node.get("title") in ctx["def_names"]:
        node["title"] = _title(path[-2]).rstrip("s") if len(path) > 1 else node["title"]
    # R9: key-derived titles (also replaces "SizeConfig" etc.)
    if (
        path
        and path[-1] != "*"
        and (
            "title" not in node
            or node["title"] in ctx["def_names"]
            or node["title"] == path[-1].replace("_", " ").title()
        )
    ):
        node["title"] = _title(path[-1])

    t = node.get("type")

    if t == "object":
        # R2b: children carry their own defaults; a whole-object default (with nulls) only
        # confuses sjsf's default merging and Ajv.
        node.pop("default", None)
        # R4
        pn = node.get("propertyNames", {})
        if "enum" in pn and isinstance(node.get("additionalProperties"), dict):
            value_schema = node.pop("additionalProperties")
            node.pop("propertyNames")
            node["properties"] = {k: copy.deepcopy(value_schema) for k in pn["enum"]}
            node["additionalProperties"] = False
        node["properties"] = {
            k: _walk(v, (*path, k), ctx) for k, v in node.get("properties", {}).items()
        }

    elif t == "array":
        pre = node.pop("prefixItems", None)
        if pre is not None:  # R3
            node["items"] = [_walk(p, (*path, str(i)), ctx) for i, p in enumerate(pre)]
            for i, item in enumerate(node["items"]):
                item["title"] = ["min", "max"][i] if len(pre) == 2 else str(i)
            node["additionalItems"] = False
            if len(pre) == 2 and all(p.get("type") in ("number", "integer") for p in pre):
                node["x-widget"] = "range-pair"
        elif isinstance(node.get("items"), dict):
            node["items"] = _walk(node["items"], (*path, "*"), ctx)

    elif t in ("number", "integer"):
        lo, hi = node.get("minimum"), node.get("maximum")
        if lo is not None and hi is not None:  # R5
            node["x-widget"] = "slider"
            node["x-step"] = _nice_step(lo, hi, t == "integer")

    elif t == "string":
        if node.get("format") == "path":  # R6
            del node["format"]
            node["x-widget"] = "path"
        cats = _match(MATERIAL_FIELDS, path)
        if cats is not None:  # R7
            node["x-widget"] = "material-select"
            node["x-options"] = [
                n for n, m in ctx["materials"].items() if m.get("category") in cats
            ]

    derived = _match(DERIVED, path)
    if derived:  # R8
        node["x-derived"] = derived
        if derived == "computed":
            node["readOnly"] = True
    return node


def postprocess_schema(
    schema: dict[str, Any], materials: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    defs = schema.get("$defs", {})
    root = _deref({k: v for k, v in schema.items() if k != "$defs"}, defs)  # R1
    out = _walk(root, (), {"materials": materials, "def_names": set(defs)})
    out["$schema"] = "http://json-schema.org/draft-07/schema#"
    # Only things that can't live in the schema go in the ui-schema.
    ui_schema = {
        "hardware": {"ui:options": {"orderable": True, "addable": True, "removable": True}},
        "dfm": {"cnc": {"tools": {"ui:options": {"orderable": True}}}},
    }
    return out, ui_schema


def restore(data: Any) -> Any:
    """Reverse pass for form output: drop None (== unset). Tuples/dicts already match."""
    if isinstance(data, dict):
        return {k: restore(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [restore(v) for v in data]
    return data


def provenance(variant: Path) -> dict[str, str]:
    """Which layer last set each leaf, as JSON pointers -> file label. Lists count as leaves."""
    # Mirrors luregen.config.loader.load_layered's `extends` chain.
    chain: list[Path] = []
    cur: Path | None = variant.resolve()
    while cur is not None:
        chain.append(cur)
        parent = tomllib.loads(cur.read_text()).get("extends")
        cur = (cur.parent / parent).resolve() if parent else None
    out: dict[str, str] = {}

    def mark(d: dict[str, Any], prefix: str, label: str) -> None:
        for k, v in d.items():
            if k == "extends":
                continue
            p = f"{prefix}/{k}"
            if isinstance(v, dict):
                mark(v, p, label)
                continue
            # Scalars and lists replace whole (loader.deep_merge), so drop older entries below.
            for stale in [q for q in out if q == p or q.startswith(p + "/")]:
                del out[stale]
            out[p] = label
            if isinstance(v, list) and all(isinstance(x, dict) for x in v):
                for i, item in enumerate(v):  # array of tables: per-item, per-key provenance
                    mark(item, f"{p}/{i}", label)

    for file in reversed(chain):
        mark(tomllib.loads(file.read_text()), "", re.sub(r"\.toml$", "", file.name))
    return out


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from luregen.config import LureConfig
    from luregen.config.resolve import load_materials

    root = Path(__file__).resolve().parents[2]
    mats = load_materials(root / "spec")
    schema, ui = postprocess_schema(LureConfig.model_json_schema(), mats)
    json.dump({"schema": schema, "uiSchema": ui}, sys.stdout, indent=1)
