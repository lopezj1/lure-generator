// Reading the form schema's editor hints (#121): groups (`x-group`, `x-groups-in`), levels
// (`x-level`) and layouts (`x-layout`, `x-rows`). The server decides all of them
// (luregen.api.formschema R10–R12); these helpers only select what a tab shows.
// Type-only imports, so `node --test` can load this file directly.
import type { Path } from '../paths';

const pointer = (path: Path) => path.map((p) => '/' + String(p)).join('');

export type Node = Record<string, unknown> & {
  type?: string | string[];
  title?: string;
  description?: string;
  properties?: Record<string, Node>;
  items?: Node | Node[];
  required?: string[];
  enum?: unknown[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  readOnly?: boolean;
  additionalProperties?: Node | boolean;
};
export type Level = 'basic' | 'advanced';
export interface Group {
  id: string;
  title: string;
  description?: string;
}
export interface FieldError {
  path: (string | number)[];
  message: string;
}

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

export function groupsOf(schema: Node): Group[] {
  const groups = schema['x-groups'];
  return Array.isArray(groups) ? (groups as Group[]) : [];
}

/** An object node that renders its children one by one (not as one table or key/value). */
export function isContainer(node: Node): boolean {
  return node.type === 'object' && isObj(node.properties) && !node['x-layout'];
}

/** Does `node` (or, for a container, any descendant) belong to `group` at `level`? */
export function shows(node: Node, group: string, level?: Level): boolean {
  if (isContainer(node)) {
    return Object.values(node.properties!).some((c) => shows(c, group, level));
  }
  return node['x-group'] === group && (level === undefined || node['x-level'] === level);
}

/** Children of a container shown in `group` at `level`, in schema order. */
export function children(node: Node, group: string, level: Level): [string, Node][] {
  return Object.entries(node.properties ?? {}).filter(([, c]) => shows(c, group, level));
}

/** Number of leaf controls (fields, tables) in `node` for `group` at `level`. */
export function countFields(node: Node, group: string, level: Level): number {
  if (isContainer(node)) {
    return Object.values(node.properties!).reduce((n, c) => n + countFields(c, group, level), 0);
  }
  return shows(node, group, level) ? 1 : 0;
}

/** Form label: the server's `x-label`, else the title, else the key. */
export function labelOf(node: Node, key: string | number = ''): string {
  return String(node['x-label'] ?? node.title ?? key);
}

export function unitOf(node: Node): string {
  return typeof node['x-unit'] === 'string' ? node['x-unit'] : '';
}

/** The schema node at a data path (array indices step into `items`). */
export function nodeAt(schema: Node, path: Path): Node | undefined {
  let node: Node | undefined = schema;
  for (const key of path) {
    if (!node) return undefined;
    if (typeof key === 'number' || /^\d+$/.test(String(key))) {
      const items: Node | Node[] | undefined = node.items;
      node = Array.isArray(items) ? items[Number(key)] : items;
    } else if (node.properties?.[key]) {
      node = node.properties[key];
    } else if (isObj(node.additionalProperties)) {
      node = node.additionalProperties as Node;
    } else {
      return undefined;
    }
  }
  return node;
}

/** Group of the field an error points at: the deepest node on its path with `x-group`. */
export function errorGroup(schema: Node, path: Path): string | null {
  let group: string | null = null;
  for (let n = 1; n <= path.length; n++) {
    const node = nodeAt(schema, path.slice(0, n));
    if (!node) break;
    // A container's own `x-group` is only a default; prefer a leaf's.
    if (typeof node['x-group'] === 'string') group = node['x-group'] as string;
  }
  return group;
}

/** Error counts per group (errors at the root count under "" ). */
export function errorCounts(schema: Node, errors: FieldError[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const e of errors) {
    const g = e.path.length ? (errorGroup(schema, e.path) ?? '') : '';
    out[g] = (out[g] ?? 0) + 1;
  }
  return out;
}

/** Errors exactly at `path`. */
export function errorsAt(errors: FieldError[], path: Path): string[] {
  const p = pointer(path);
  return errors.filter((e) => pointer(e.path) === p).map((e) => e.message);
}

/** Errors at `path` or below it. */
export function errorsUnder(errors: FieldError[], path: Path): FieldError[] {
  const p = pointer(path);
  return errors.filter((e) => {
    const q = pointer(e.path);
    return q === p || q.startsWith(p + '/');
  });
}

/** Columns of a table of row objects: the keys every row shares, in the first row's order. */
export function sharedKeys(rows: Node[]): string[] {
  if (!rows.length) return [];
  const [first, ...rest] = rows;
  return Object.keys(first.properties ?? {}).filter((k) =>
    rest.every((r) => r.properties && k in r.properties),
  );
}

/** A value for a new array row: schema defaults, the first option of required enums. */
export function newRow(item: Node): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  const required = new Set(item.required ?? []);
  for (const [k, c] of Object.entries(item.properties ?? {})) {
    if (c.default !== undefined && c.default !== null) out[k] = structuredClone(c.default);
    else if (required.has(k) && Array.isArray(c.enum) && c.enum.length) out[k] = c.enum[0];
  }
  return out;
}

/** Parse a key/value cell (`print.overrides` takes str | int | float | bool). */
export function parseLoose(text: string): string | number | boolean {
  const t = text.trim();
  if (t === 'true') return true;
  if (t === 'false') return false;
  if (t !== '' && !Number.isNaN(Number(t))) return Number(t);
  return text;
}
