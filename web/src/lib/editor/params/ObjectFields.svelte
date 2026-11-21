<!-- The fields of one config object for a tab and level (#121): scalars as a compact labeled
     form, nested objects as sub-fieldsets, and the server's table layouts as tables. -->
<script lang="ts">
  import type { Path } from '../paths';
  import ArrayTable from './ArrayTable.svelte';
  import Field from './Field.svelte';
  import KeyValueTable from './KeyValueTable.svelte';
  import ObjectFields from './ObjectFields.svelte';
  import RowTable from './RowTable.svelte';
  import { children, isContainer, labelOf, sharedKeys, type Level, type Node } from './schema';

  let {
    path,
    node,
    group,
    level,
    exclude = [],
  }: { path: Path; node: Node; group: string; level: Level; exclude?: string[] } = $props();

  const shown = $derived(children(node, group, level).filter(([k]) => !exclude.includes(k)));
  const required = $derived(new Set(node.required ?? []));
  // `x-rows`: those child objects render as one table; what they don't share goes below.
  const rowKeys = $derived(
    Array.isArray(node['x-rows']) ? (node['x-rows'] as string[]) : ([] as string[]),
  );
  const rows = $derived(shown.filter(([k]) => rowKeys.includes(k)));
  const shared = $derived(sharedKeys(rows.map(([, r]) => r)));
  const others = $derived(shown.filter(([k]) => !rowKeys.includes(k)));
</script>

<div class="fields">
  {#if rows.length}
    <div class="wide">
      <RowTable {path} {rows} title="" />
    </div>
    {#each rows as [k, r] (k)}
      {#if Object.keys(r.properties ?? {}).some((c) => !shared.includes(c))}
        <fieldset class="sub wide">
          <legend>{labelOf(r, k)}</legend>
          <ObjectFields path={[...path, k]} node={r} {group} {level} exclude={shared} />
        </fieldset>
      {/if}
    {/each}
  {/if}
  {#each others as [k, c] (k)}
    {#if isContainer(c)}
      <fieldset class="sub wide">
        <legend title={c.description ?? ''}>{labelOf(c, k)}</legend>
        <ObjectFields path={[...path, k]} node={c} {group} {level} />
      </fieldset>
    {:else if c['x-layout'] === 'table' && c.type === 'array'}
      <div class="wide"><ArrayTable path={[...path, k]} node={c} /></div>
    {:else if c['x-layout'] === 'table'}
      <div class="wide">
        <RowTable
          path={[...path, k]}
          rows={Object.entries(c.properties ?? {})}
          title={labelOf(c, k)}
          description={c.description ?? ''}
        />
      </div>
    {:else if c['x-layout'] === 'keyvalue'}
      <div class="wide"><KeyValueTable path={[...path, k]} node={c} /></div>
    {:else}
      <Field path={[...path, k]} node={c} required={required.has(k)} />
    {/if}
  {/each}
</div>
