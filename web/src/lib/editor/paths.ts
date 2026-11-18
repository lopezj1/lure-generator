// Pure helpers for the config editor: JSON-pointer access, deep equality and the save payload.
// No imports, so `node --test web/tests/` can load this file directly (type stripping).

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Path = ReadonlyArray<string | number>;
/** JSON pointer → index into `layers` (from `GET /api/variants/{family}/{name}`). */
export type Provenance = Record<string, number>;

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);

export function pointer(path: Path): string {
  return path.map((p) => '/' + String(p)).join('');
}

export function getAt(data: unknown, path: Path): unknown {
  let node: unknown = data;
  for (const key of path) {
    if (Array.isArray(node)) node = node[Number(key)];
    else if (isObj(node)) node = node[String(key)];
    else return undefined;
  }
  return node;
}

/** Set (or with `undefined`, delete) the value at `path`, creating parent tables as needed. */
export function setAt(data: Obj, path: Path, value: unknown): void {
  if (!path.length) return;
  let node: unknown = data;
  for (const key of path.slice(0, -1)) {
    const container = node as Obj;
    if (!isObj(container[key as string]) && !Array.isArray(container[key as string])) {
      if (value === undefined) return;
      container[key as string] = {};
    }
    node = container[key as string];
  }
  const last = path[path.length - 1];
  if (Array.isArray(node)) {
    node[Number(last)] = value;
  } else if (isObj(node)) {
    if (value === undefined) delete node[String(last)];
    else node[String(last)] = value;
  }
}

/** Structural equality; `null` and `undefined` both mean "unset". */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a == null || b == null) return a == null && b == null;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((x, i) => deepEqual(x, b[i]));
  }
  if (isObj(a) && isObj(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const k of keys) if (!deepEqual(a[k], b[k])) return false;
    return true;
  }
  return a === b;
}

export function clone<T>(value: T): T {
  return value === undefined ? value : (JSON.parse(JSON.stringify(value)) as T);
}

/** Paths (with `*` for array items) of every `x-derived` field in the form schema. */
export function derivedFields(schema: unknown, at: string[] = []): [string[], string][] {
  if (!isObj(schema)) return [];
  const out: [string[], string][] = [];
  if (typeof schema['x-derived'] === 'string') out.push([at, schema['x-derived']]);
  if (isObj(schema.properties)) {
    for (const [k, v] of Object.entries(schema.properties))
      out.push(...derivedFields(v, [...at, k]));
  }
  if (isObj(schema.items)) out.push(...derivedFields(schema.items, [...at, '*']));
  return out;
}

/** Drop `undefined`/`null` (unset) recursively, like the server's `restore()`. */
export function prune(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(prune);
  if (isObj(value)) {
    const out: Obj = {};
    for (const [k, v] of Object.entries(value)) if (v != null) out[k] = prune(v);
    return out;
  }
  return value;
}

/**
 * What to send to `/validate` and `PUT`: the form value without fields the server fills in.
 *
 * - `x-derived: "computed"` fields are always dropped.
 * - `x-derived: "catalog"` fields are kept only when a layer sets them or the user changed
 *   them. Otherwise the loaded (catalog-filled) value would be written back, and e.g. a stale
 *   `process.material_density` would stick after a material change.
 * - Catalog fields of array items (`hardware[].mass_g`, …) are compared with the loaded item
 *   that has the same `id` (same index first), so reordering doesn't bake catalog values in
 *   and changing an item's `id` drops the old catalog values.
 */
export function savePayload(
  value: unknown,
  initial: unknown,
  provenance: Provenance,
  derived: [string[], string][],
): Obj {
  const out = clone(prune(value)) as Obj;
  for (const [path, kind] of derived) {
    const star = path.indexOf('*');
    if (star < 0) {
      if (kind === 'computed') setAt(out, path, undefined);
      else if (!(pointer(path) in provenance) && deepEqual(getAt(out, path), getAt(initial, path)))
        setAt(out, path, undefined);
      continue;
    }
    const listPath = path.slice(0, star);
    const rest = path.slice(star + 1);
    const items = getAt(out, listPath);
    const before = getAt(initial, listPath);
    if (!Array.isArray(items)) continue;
    const old = Array.isArray(before) ? before : [];
    items.forEach((item, i) => {
      if (!isObj(item)) return;
      if (kind === 'computed') return setAt(item, rest, undefined);
      const id = item.id;
      let j = isObj(old[i]) && old[i].id === id ? i : -1;
      if (j < 0 && id != null) j = old.findIndex((o) => isObj(o) && o.id === id);
      const mine = getAt(item, rest);
      let keep: boolean;
      if (j >= 0) {
        keep =
          pointer([...listPath, j, ...rest]) in provenance || !deepEqual(mine, getAt(old[j], rest));
      } else {
        keep = mine != null && !deepEqual(mine, getAt(old[i], rest));
      }
      if (!keep) setAt(item, rest, undefined);
    });
  }
  return out;
}

/** `key.path=value` (the CLI's `--set` syntax, checked like the job runner does). */
export const OVERRIDE_RE = /^[A-Za-z_][A-Za-z0-9_.]*=[^\r\n\0]*$/;

/** `builds/<family>/<variant>/<stamp>` (absolute or relative) → its parts. */
export function parseBuildDir(
  dir: string,
): { path: string; family: string; variant: string; stamp: string } | null {
  const parts = dir.replace(/\\/g, '/').split('/').filter(Boolean);
  const at = parts.lastIndexOf('builds');
  if (at < 0 || parts.length - at !== 4) return null;
  const [family, variant, stamp] = parts.slice(at + 1);
  return { path: `builds/${family}/${variant}/${stamp}`, family, variant, stamp };
}
