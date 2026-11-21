// The design/variant being edited (#121): picked in the header, read by the editor, the
// fab bars and the builds page. `editorDirty` lets the header ask before dropping edits.
import { writable } from 'svelte/store';
import type { components } from '../api';

export type DesignSummary = components['schemas']['DesignSummary'];

export const designs = writable<DesignSummary[]>([]);
export const selection = writable<{ family: string; variant: string }>({
  family: '',
  variant: '',
});
export const editorDirty = writable(false);
