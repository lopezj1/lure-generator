<!-- The variant's parameters as group tabs (#121). Tabs, levels and layouts come from the
     form schema's hints (`x-groups`, `x-group`, `x-level`, `x-layout`); every schema field
     renders in some tab, and the editor's value is the whole config, so fields a tab doesn't
     show still round-trip on save. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { getEditor } from '../state.svelte';
  import Section from './Section.svelte';
  import { countFields, errorCounts, groupsOf, isContainer, shows, type Node } from './schema';

  let {
    schema,
    group,
    onpick,
    extras,
  }: {
    schema: Node;
    group: string;
    onpick: (group: string) => void;
    extras?: Snippet<[string]>;
  } = $props();
  const editor = getEditor();

  const groups = $derived(groupsOf(schema));
  const active = $derived(groups.some((g) => g.id === group) ? group : (groups[0]?.id ?? ''));
  const counts = $derived(errorCounts(schema, editor.errors));
  const current = $derived(groups.find((g) => g.id === active));

  interface Card {
    key: string;
    path: (string | number)[];
    node: Node;
    title?: string;
  }

  /** Cards for a tab: one per top-level object; objects spanning several tabs (dfm) split
   *  into their children; top-level scalars (name, family) share a "General" card. */
  const cards = $derived.by((): Card[] => {
    const out: Card[] = [];
    const loose: [string, Node][] = [];
    const visit = (node: Node, path: string[]) => {
      for (const [k, c] of Object.entries(node.properties ?? {})) {
        if (!shows(c, active)) continue;
        const spans = (c['x-groups-in'] as string[] | undefined)?.length ?? 1;
        if (isContainer(c) && spans > 1) visit(c, [...path, k]);
        else if (!isContainer(c) && !c['x-layout'] && path.length === 0) loose.push([k, c]);
        else out.push({ key: [...path, k].join('.'), path: [...path, k], node: c });
      }
    };
    visit(schema, []);
    // Cards with basic fields first; expert-only cards (collapsed) after them.
    const expertOnly = (c: Card) => isContainer(c.node) && !countFields(c.node, active, 'basic');
    out.sort((a, b) => Number(expertOnly(a)) - Number(expertOnly(b)));
    if (loose.length) {
      const node: Node = { type: 'object', properties: Object.fromEntries(loose) };
      out.unshift({ key: '_general', path: [], node, title: 'General' });
    }
    return out;
  });
</script>

<div class="param-editor">
  <div class="group-tabs" role="tablist" aria-label="Parameter groups">
    {#each groups as g (g.id)}
      <button
        type="button"
        role="tab"
        aria-selected={g.id === active}
        class:active={g.id === active}
        title={g.description ?? ''}
        onclick={() => onpick(g.id)}
      >
        {g.title}
        {#if counts[g.id]}<span class="badge error">{counts[g.id]}</span>{/if}
      </button>
    {/each}
  </div>

  {#if current?.description}<p class="group-desc muted">{current.description}</p>{/if}
  {@render extras?.(active)}

  <div class="cards" role="tabpanel">
    {#each cards as c (c.key)}
      <Section {schema} path={c.path} node={c.node} group={active} title={c.title} />
    {/each}
  </div>
</div>
