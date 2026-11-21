<!-- One card of a parameter tab (#121): a top-level config object (or a table). Basic fields
     show; advanced ones sit in a collapsed "More … settings" part, which opens by itself when
     the server reports an error in it. A card with only advanced fields starts collapsed. -->
<script lang="ts">
  import type { Path } from '../paths';
  import { getEditor } from '../state.svelte';
  import ArrayTable from './ArrayTable.svelte';
  import Field from './Field.svelte';
  import KeyValueTable from './KeyValueTable.svelte';
  import ObjectFields from './ObjectFields.svelte';
  import RowTable from './RowTable.svelte';
  import {
    countFields,
    errorGroup,
    errorsAt,
    errorsUnder,
    isContainer,
    labelOf,
    nodeAt,
    type Node,
  } from './schema';

  let {
    schema,
    path,
    node,
    group,
    title,
  }: { schema: Node; path: Path; node: Node; group: string; title?: string } = $props();
  const editor = getEditor();

  const label = $derived(title ?? labelOf(node, String(path.at(-1) ?? '')));
  const container = $derived(isContainer(node));
  const basic = $derived(container ? countFields(node, group, 'basic') : 1);
  const advanced = $derived(container ? countFields(node, group, 'advanced') : 0);
  const wide = $derived(
    !container ||
      Object.values(node.properties ?? {}).some(
        (c) => c['x-layout'] === 'table' && c.type === 'array' && c['x-level'] === 'basic',
      ),
  );
  const sectionErrors = $derived(path.length ? errorsAt(editor.errors, path) : []);
  const advancedErrors = $derived(
    errorsUnder(editor.errors, path).filter(
      (e) =>
        e.path.length > path.length &&
        errorGroup(schema, e.path) === group &&
        nodeAt(schema, e.path)?.['x-level'] === 'advanced',
    ).length,
  );

  let open = $state(false);
  $effect(() => {
    if (advancedErrors > 0) open = true;
  });
</script>

<section class="card" class:wide class:only-advanced={!basic}>
  {#if basic}
    {#if container || !node['x-layout']}<h3 title={node.description ?? ''}>{label}</h3>{/if}
    {#each sectionErrors as e (e)}<small class="error-text">{e}</small>{/each}
    {#if !container && node['x-layout'] === 'table' && node.type === 'array'}
      <ArrayTable {path} {node} />
    {:else if !container && node['x-layout'] === 'table'}
      <RowTable {path} rows={Object.entries(node.properties ?? {})} title={label} />
    {:else if !container && node['x-layout'] === 'keyvalue'}
      <KeyValueTable {path} {node} />
    {:else if !container}
      <Field {path} {node} />
    {:else}
      <ObjectFields {path} {node} {group} level="basic" />
    {/if}
  {/if}
  {#if advanced}
    <details class="more" bind:open>
      <summary>
        {#if basic}More {label.toLowerCase()} settings{:else}<strong>{label}</strong>{/if}
        <small class="muted">{advanced} {advanced === 1 ? 'setting' : 'settings'}</small>
        {#if advancedErrors}<span class="badge error">{advancedErrors} error(s)</span>{/if}
      </summary>
      {#if !basic}
        {#each sectionErrors as e (e)}<small class="error-text">{e}</small>{/each}
      {/if}
      <ObjectFields {path} {node} {group} level="advanced" />
    </details>
  {/if}
</section>
