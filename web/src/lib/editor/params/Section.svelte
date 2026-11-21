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
  import { tip } from './tip';
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
    exclude = [],
    wide = false,
  }: {
    schema: Node;
    path: Path;
    node: Node;
    group: string;
    title?: string;
    exclude?: string[];
    wide?: boolean;
  } = $props();
  const editor = getEditor();

  const label = $derived(title ?? labelOf(node, String(path.at(-1) ?? '')));
  const container = $derived(isContainer(node));
  // Counts leave out the children shown as cards of their own.
  const count = (level: 'basic' | 'advanced') =>
    Object.entries(node.properties ?? {})
      .filter(([k]) => !exclude.includes(k))
      .reduce((n, [, c]) => n + countFields(c, group, level), 0);
  const basic = $derived(container ? count('basic') : 1);
  const advanced = $derived(container ? count('advanced') : 0);
  const sectionErrors = $derived(path.length ? errorsAt(editor.errors, path) : []);
  const advancedErrors = $derived(
    errorsUnder(editor.errors, path).filter(
      (e) =>
        e.path.length > path.length &&
        !exclude.includes(String(e.path[path.length])) &&
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
    {#if container || !node['x-layout']}<h3 use:tip={{ text: node.description }}>{label}</h3>{/if}
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
      <ObjectFields {path} {node} {group} {exclude} level="basic" />
    {/if}
  {/if}
  {#if advanced}
    <details class="more" bind:open>
      <summary>
        {#if basic}More {label.split(' · ').at(-1)?.toLowerCase()} settings{:else}<strong>
            {label}
          </strong>{/if}
        <small class="muted">{advanced} {advanced === 1 ? 'setting' : 'settings'}</small>
        {#if advancedErrors}<span class="badge error">{advancedErrors} error(s)</span>{/if}
      </summary>
      {#if !basic}
        {#each sectionErrors as e (e)}<small class="error-text">{e}</small>{/each}
      {/if}
      <ObjectFields {path} {node} {group} {exclude} level="advanced" />
    </details>
  {/if}
</section>
