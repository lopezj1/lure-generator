"""Write an edited config back to a variant TOML as a minimal diff (#62).

The editor sends a whole config (possibly with defaults and catalog values filled in). The
variant file must keep only what differs from its parents, with its comments and layout:

1. Merge the parent chain (`load_layered` of the `extends` target).
2. Diff the edited config against the parents. Tables recurse; scalars and lists (including
   `[[hardware]]`) compare whole.
3. Drop keys the variant didn't have that change nothing after validation and `resolve()`
   (pydantic defaults and catalog-filled values the form echoes back).
4. Apply the result to the tomlkit document: change keys in place, add new keys at the end of
   their table, delete keys that now equal the inherited value. `extends` and `name` are never
   touched, and path keys are written relative to the variant file.

All paths in `edited` are absolute; `load_layered` makes the parents' paths absolute too.
"""

import copy
import os
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import AoT, InlineTable, Item, Table
from tomlkit.toml_document import TOMLDocument

from luregen.config.loader import _PATH_KEYS, _absolutize_paths, deep_merge, load_layered

PROTECTED = ("extends", "name")
_MISSING = object()

Key = tuple[str | int, ...]


def _is_table_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def diff(edited: dict[str, Any], parents: dict[str, Any]) -> dict[str, Any]:
    """Keys of `edited` whose value differs from `parents` (tables recurse, the rest whole)."""
    out: dict[str, Any] = {}
    for key, value in edited.items():
        base = parents.get(key, _MISSING)
        if isinstance(value, dict) and isinstance(base, dict):
            sub = diff(value, base)
            if sub:
                out[key] = sub
        elif base is _MISSING or value != base:
            out[key] = copy.deepcopy(value)
    return out


def _candidates(desired: Any, own: Any, prefix: Key = ()) -> Iterator[Key]:
    """Leaf keys of `desired` the variant file doesn't have yet (list items by index)."""
    for key, value in desired.items():
        path = (*prefix, key)
        mine = own.get(key, _MISSING) if isinstance(own, dict) else _MISSING
        if isinstance(value, dict):
            yield from _candidates(value, mine if isinstance(mine, dict) else {}, path)
        elif _is_table_list(value):
            mine_list = mine if isinstance(mine, list) else []
            if mine is _MISSING:
                yield path
            for i, item in enumerate(value):
                old = mine_list[i] if i < len(mine_list) else {}
                yield from _candidates(item, old if isinstance(old, dict) else {}, (*path, i))
        elif mine is _MISSING:
            yield path


def _without(data: dict[str, Any], path: Key) -> dict[str, Any]:
    out = copy.deepcopy(data)
    node: Any = out
    for part in path[:-1]:
        node = node[part]
    del node[path[-1]]
    return _prune(out)


def _prune(data: dict[str, Any]) -> dict[str, Any]:
    """Drop tables left empty (list items stay, even when empty)."""
    for key in list(data):
        value = data[key]
        if isinstance(value, dict):
            _prune(value)
            if not value:
                del data[key]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _prune(item)
    return data


def minimize(
    desired: dict[str, Any], own: dict[str, Any], same: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """Drop new keys one at a time while `same(candidate)` says the result is unchanged."""
    for path in reversed(list(_candidates(desired, own))):
        try:
            trial = _without(desired, path)
        except (KeyError, IndexError):  # already pruned with its table
            continue
        if same(trial):
            desired = trial
    return desired


# --- tomlkit document edits ----------------------------------------------------------------


def _clean(value: Any) -> Any:
    """Integral floats as ints (JSON gives 175.0 for `length_mm = 175`)."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _to_item(value: Any, inline: bool) -> Any:
    value = _clean(value)
    if isinstance(value, dict):
        if inline:
            table = tomlkit.inline_table()
            for k, v in value.items():
                table[k] = _to_item(v, True)
            return table
        return _table(value)
    if _is_table_list(value) and not inline:
        aot = tomlkit.aot()
        for item in value:
            aot.append(_table(item))
        return aot
    if isinstance(value, list):
        array = tomlkit.array()
        array.extend(_to_item(v, True) for v in value)
        return array
    return value


def _table(value: dict[str, Any]) -> Table:
    nested = {k: v for k, v in value.items() if isinstance(v, dict) or _is_table_list(v)}
    table = tomlkit.table(is_super_table=bool(value) and len(nested) == len(value))
    for k, v in value.items():
        if k not in nested:
            table.add(k, _to_item(v, False))
    for k, v in nested.items():
        table.add(k, _to_item(v, False))
    return table


def _insert(node: Any, key: str, value: Any) -> None:
    """Add `key` at the end of `node`'s own key/value pairs (before sub-tables and trailing
    comments)."""
    inline = isinstance(node, InlineTable)
    item = _to_item(value, inline)
    if inline or isinstance(item, (Table, AoT)):
        node.append(key, item)
        return
    container = node.value if isinstance(node, Table) else node
    last = None
    for k, it in container.body:
        if k is not None and isinstance(it, Item) and not isinstance(it, (Table, AoT)):
            last = k
    if last is not None:
        container._insert_after(last, key, item)
    elif container.body:
        container._insert_at(0, key, item)
    else:
        container.append(key, item)


def _plain(value: Any) -> Any:
    return value.unwrap() if hasattr(value, "unwrap") else value


def sync(node: Any, want: dict[str, Any], top: bool = True) -> None:
    """Make the tomlkit table `node` hold exactly `want`, editing as little as possible."""
    for key in list(node.keys()):
        if not (top and key in PROTECTED) and key not in want:
            del node[key]
    for key, value in want.items():
        if top and key in PROTECTED:
            continue
        if key not in node:
            _insert(node, key, value)
            continue
        current = node[key]
        if isinstance(value, dict) and isinstance(current, dict):
            sync(current, value, top=False)
        elif _is_table_list(value) and isinstance(current, AoT) and len(current) == len(value):
            for table, item in zip(current, value, strict=True):
                sync(table, item, top=False)
        elif _plain(current) != value:
            node[key] = _to_item(value, isinstance(node, InlineTable))


# --- whole-file rewrite --------------------------------------------------------------------


def _path_strings(
    desired: dict[str, Any], own_raw: dict[str, Any], own_abs: dict[str, Any], base: Path
) -> None:
    """Write path keys relative to the variant file, keeping the file's own spelling."""
    for section, key in _PATH_KEYS:
        table = desired.get(section)
        if not isinstance(table, dict) or not isinstance(table.get(key), str):
            continue
        if (own_abs.get(section) or {}).get(key) == table[key]:
            table[key] = own_raw[section][key]
        else:
            table[key] = Path(os.path.relpath(table[key], base)).as_posix()


def rewrite_variant(
    path: Path, edited: dict[str, Any], resolved_of: Callable[[dict[str, Any]], Any]
) -> str:
    """New text for variant `path` holding `edited` (absolute paths) as a minimal diff.

    `resolved_of(layered_dict)` validates and resolves a merged config; its results are
    compared to decide which echoed defaults can be dropped (raise or return None if invalid).
    """
    text = path.read_text()
    doc: TOMLDocument = tomlkit.parse(text)
    own_raw = tomllib.loads(text)
    own_abs = _absolutize_paths(copy.deepcopy(own_raw), path.parent.resolve())
    parent = own_raw.get("extends")
    parents = load_layered(path.parent / parent) if parent else {}
    fixed = {k: own_raw[k] for k in PROTECTED if k in own_raw and k != "extends"}

    def merged(desired: dict[str, Any]) -> dict[str, Any]:
        return deep_merge(parents, {**desired, **fixed})

    def safe(desired: dict[str, Any]) -> Any:
        try:
            return resolved_of(merged(desired))
        except ValueError:
            return None

    edited = {k: v for k, v in edited.items() if k not in PROTECTED}
    desired = diff(edited, parents)
    target = safe(desired)
    if target is not None:
        own_body = {k: v for k, v in own_abs.items() if k not in PROTECTED}
        desired = minimize(desired, own_body, lambda d: safe(d) == target)
    _path_strings(desired, own_raw, own_abs, path.parent.resolve())
    sync(doc, desired)
    out = doc.as_string()
    if out != text:  # removed trailing tables leave their blank separator behind
        out = out.rstrip() + "\n"
    return out
