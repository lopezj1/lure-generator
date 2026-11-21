// Editor state shared with the parameter fields through Svelte context: the live value
// (edited in place), the server's validation errors, what was loaded, what the parent layers
// give, and where each value came from. Marks and resets read it; the page builds the save
// payload from it.
import { getContext, setContext } from 'svelte';
import type { components } from '../api';
import {
  clone,
  deepEqual,
  derivedFields,
  getAt,
  pointer,
  savePayload,
  setAt,
  type Path,
} from './paths';

type VariantDetail = components['schemas']['VariantDetail'];
type FieldError = components['schemas']['FieldError'];
type Obj = Record<string, unknown>;

const KEY = Symbol('lure-editor');

/** Badge label for a value with no layer entry. */
export type Origin = 'variant' | 'family' | 'common' | 'default' | 'catalog' | string;

export class EditorState {
  value: Obj = $state({});
  /** Server validation errors (`/validate`, 422 on save), shown inline by path. */
  errors: FieldError[] = $state([]);
  readonly initial: Obj;
  readonly inherited: Obj;
  readonly detail: VariantDetail;
  readonly derived: [string[], string][];

  constructor(detail: VariantDetail, schema: unknown) {
    this.detail = detail;
    // `resolved` is the full config; an invalid variant falls back to its merged layers.
    this.initial = clone((detail.resolved ?? detail.layered) as Obj);
    this.inherited = detail.inherited ?? {};
    this.value = clone(this.initial);
    this.derived = derivedFields(schema);
    this.errors = detail.errors;
  }

  get(path: Path): unknown {
    return getAt(this.value, path);
  }

  /** Set a value; `undefined` unsets it (the default / parent layers apply). */
  set(path: Path, value: unknown): void {
    setAt(this.value, path, value);
  }

  /** Where the value at `path` comes from, for tooltips. */
  originText(path: Path, schema: Obj): string {
    const origin = this.origin(path, schema);
    const layer = this.detail.layers.find((l) => l.label === origin);
    return layer ? `${origin} (${layer.path})` : origin;
  }

  get family(): string {
    return this.detail.family;
  }

  /** Which layer set `path` ("variant", "family", "common", …), else "catalog"/"default". */
  origin(path: Path, schema: Obj): Origin {
    const prov = this.detail.provenance;
    let idx = prov[pointer(path)];
    // A list item without its own entry inherits its list's layer.
    if (idx === undefined && path.length && /^\d+$/.test(String(path[path.length - 1]))) {
      idx = prov[pointer(path.slice(0, -1))];
    }
    if (idx !== undefined) return this.detail.layers[idx]?.label ?? 'variant';
    return schema['x-derived'] ? 'catalog' : 'default';
  }

  isEdited(path: Path): boolean {
    return !deepEqual(getAt(this.value, path), getAt(this.initial, path));
  }

  /** "Reset to inherited" makes sense outside lists, when the variant (or an edit) sets a
   *  value that differs from what the parent layers (or the default/catalog) give. */
  canReset(path: Path, schema: Obj): boolean {
    if (path.some((p) => typeof p === 'number' || /^\d+$/.test(String(p)))) return false;
    if (schema.readOnly || (path.length === 1 && path[0] === 'name')) return false;
    const inherited = getAt(this.inherited, path);
    if (inherited === undefined && schema.type === 'array' && !schema['x-nullable']) return false;
    const ownOrEdited = this.origin(path, schema) === 'variant' || this.isEdited(path);
    return ownOrEdited && !deepEqual(getAt(this.value, path), inherited);
  }

  /** Back to the parent layers' value; unset (default / catalog) when they don't set it. */
  reset(path: Path): void {
    setAt(this.value, path, clone(getAt(this.inherited, path)));
  }

  /** Back to the value as loaded. */
  revert(path: Path): void {
    setAt(this.value, path, clone(getAt(this.initial, path)));
  }

  get dirty(): boolean {
    return !deepEqual(this.value, this.initial);
  }

  /** The config to send to `/validate` and `PUT` (see `savePayload`). */
  payload(): Obj {
    return savePayload(
      $state.snapshot(this.value),
      this.initial,
      this.detail.provenance,
      this.derived,
    );
  }
}

export function setEditor(state: EditorState): EditorState {
  return setContext(KEY, state);
}

export function getEditor(): EditorState {
  return getContext<EditorState>(KEY);
}
