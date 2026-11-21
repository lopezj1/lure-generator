<!-- An array of records (`x-layout: "table"` on an array: hardware, air pockets) as a table
     with editable cells (#121). Basic columns show; the advanced ones (e.g. hardware catalog
     values) behind a toggle. Rows can be added, duplicated, moved and removed. -->
<script lang="ts">
  import type { Path } from '../paths';
  import { getEditor } from '../state.svelte';
  import Field from './Field.svelte';
  import FieldMeta from './FieldMeta.svelte';
  import { tip } from './tip';
  import { errorsAt, labelOf, newRow, type Node } from './schema';

  let { path, node }: { path: Path; node: Node } = $props();
  const editor = getEditor();

  let showAll = $state(false);

  const item = $derived(node.items as Node);
  const required = $derived(new Set(item.required ?? []));
  const allCols = $derived(Object.entries(item.properties ?? {}));
  const basicCols = $derived(allCols.filter(([, c]) => c['x-level'] !== 'advanced'));
  const cols = $derived(showAll || !basicCols.length ? allCols : basicCols);
  const hidden = $derived(allCols.length - cols.length);
  const rows = $derived((editor.get(path) as Record<string, unknown>[] | undefined) ?? []);
  const errors = $derived(errorsAt(editor.errors, path));

  function header(c: Node, k: string): string {
    const unit = typeof c['x-unit'] === 'string' ? ` (${c['x-unit']})` : '';
    return labelOf(c, k) + unit;
  }

  function list(): Record<string, unknown>[] {
    const v = editor.get(path);
    if (Array.isArray(v)) return v as Record<string, unknown>[];
    editor.set(path, []);
    return editor.get(path) as Record<string, unknown>[];
  }

  function add() {
    list().push(newRow(item));
  }
  function duplicate(i: number) {
    const l = list();
    l.splice(i + 1, 0, $state.snapshot(l[i]));
  }
  function move(i: number, by: number) {
    const l = list();
    const j = i + by;
    if (j < 0 || j >= l.length) return;
    const [row] = l.splice(i, 1);
    l.splice(j, 0, $state.snapshot(row));
  }
  function remove(i: number) {
    list().splice(i, 1);
  }
</script>

<div class="array-table">
  <div class="table-head">
    <span class="title" use:tip={{ text: node.description }}>
      {labelOf(node, String(path.at(-1)))}
    </span>
    <FieldMeta {path} {node} />
    <small class="muted">{rows.length} {rows.length === 1 ? 'row' : 'rows'}</small>
    <span class="spacer"></span>
    {#if allCols.length !== basicCols.length && basicCols.length}
      <label class="toggle">
        <input type="checkbox" role="switch" bind:checked={showAll} />
        <small>{showAll ? 'All columns' : `+${hidden} more columns`}</small>
      </label>
    {/if}
  </div>
  {#each errors as e (e)}<small class="error-text">{e}</small>{/each}
  <div class="scroll">
    <table>
      <thead>
        <tr>
          <th class="idx">#</th>
          {#each cols as [k, c] (k)}
            <th use:tip={{ text: c.description }}>{header(c, k)}</th>
          {/each}
          <th class="ops"><span class="sr">Row actions</span></th>
        </tr>
      </thead>
      <tbody>
        {#each rows, i (i)}
          {@const rowErrors = errorsAt(editor.errors, [...path, i])}
          <tr>
            <td class="idx">{i + 1}</td>
            {#each cols as [k, c] (k)}
              <td>
                <Field path={[...path, i, k]} node={c} compact required={required.has(k)} />
              </td>
            {/each}
            <td class="ops">
              <button
                type="button"
                class="mini"
                title="Move up"
                aria-label="Move row {i + 1} up"
                disabled={i === 0}
                onclick={() => move(i, -1)}
              >
                ↑
              </button>
              <button
                type="button"
                class="mini"
                title="Move down"
                aria-label="Move row {i + 1} down"
                disabled={i === rows.length - 1}
                onclick={() => move(i, 1)}
              >
                ↓
              </button>
              <button
                type="button"
                class="mini"
                title="Duplicate"
                aria-label="Duplicate row {i + 1}"
                onclick={() => duplicate(i)}
              >
                copy
              </button>
              <button
                type="button"
                class="mini"
                title="Remove"
                aria-label="Remove row {i + 1}"
                onclick={() => remove(i)}
              >
                ✕
              </button>
            </td>
          </tr>
          {#if rowErrors.length}
            <tr class="row-errors">
              <td></td>
              <td colspan={cols.length + 1}>
                {#each rowErrors as e (e)}<small class="error-text">{e}</small>{/each}
              </td>
            </tr>
          {/if}
        {:else}
          <tr><td colspan={cols.length + 2}><small class="muted">No rows.</small></td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  <button type="button" class="mini add" onclick={add}>+ Add row</button>
</div>
