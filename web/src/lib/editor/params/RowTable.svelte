<!-- Fixed rows as one table (#121): the keys of an `x-layout: "table"` object (sink bands:
     rows × min/max) or the child objects named by `x-rows` (internals head/tail, CAM ops:
     rows × the columns every row shares). Row-specific extras render under the table. -->
<script lang="ts">
  import type { Path } from '../paths';
  import { getEditor } from '../state.svelte';
  import Field from './Field.svelte';
  import { tip } from './tip';
  import { errorsAt, labelOf, sharedKeys, type Node } from './schema';

  let {
    path,
    rows,
    title,
    description = '',
  }: { path: Path; rows: [string, Node][]; title: string; description?: string } = $props();
  const editor = getEditor();

  // Rows that are tuples ([min, max]) use the tuple slots as columns.
  const tuple = $derived(rows.every(([, r]) => Array.isArray(r.items)));
  const cols = $derived.by((): [string | number, string, (r: Node) => Node, string?][] => {
    if (tuple) {
      const items = rows[0]?.[1].items as Node[];
      return (items ?? []).map((c, i) => [i, labelOf(c, i), (r) => (r.items as Node[])[i]]);
    }
    return sharedKeys(rows.map(([, r]) => r)).map((k) => {
      const c = rows[0][1].properties![k];
      const unit = typeof c['x-unit'] === 'string' ? ` (${c['x-unit']})` : '';
      return [k, labelOf(c, k) + unit, (r) => r.properties![k], c.description];
    });
  });
  const unit = $derived(tuple ? ((rows[0]?.[1]['x-unit'] as string) ?? '') : '');
</script>

<div class="row-table">
  {#if title}
    <div class="table-head">
      <span class="title" use:tip={{ text: description }}>{title}</span>
      {#if unit}<small class="muted">{unit}</small>{/if}
    </div>
  {/if}
  {#each errorsAt(editor.errors, path) as e (e)}<small class="error-text">{e}</small>{/each}
  <div class="scroll">
    <table>
      <thead>
        <tr>
          <th></th>
          {#each cols as [k, label, , help] (k)}<th use:tip={{ text: help }}>{label}</th>{/each}
        </tr>
      </thead>
      <tbody>
        {#each rows as [rk, r] (rk)}
          {@const rowErrors = errorsAt(editor.errors, [...path, rk])}
          <tr>
            <th scope="row" use:tip={{ text: r.description }}>{labelOf(r, rk)}</th>
            {#each cols as [k, label, cell] (k)}
              <td>
                <Field
                  path={[...path, rk, k]}
                  node={cell(r)}
                  compact
                  required
                  label="{labelOf(r, rk)} {label}"
                />
              </td>
            {/each}
          </tr>
          {#if rowErrors.length}
            <tr class="row-errors">
              <td></td>
              <td colspan={cols.length}>
                {#each rowErrors as e (e)}<small class="error-text">{e}</small>{/each}
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  </div>
</div>
