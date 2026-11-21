<!-- The variant's parameters as group tabs (#121). Tabs, levels and layouts come from the
     form schema's hints (`x-groups`, `x-group`, `x-level`, `x-layout`); every schema field
     renders in some tab, and the editor's value is the whole config, so fields a tab doesn't
     show still round-trip on save. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { getEditor } from '../state.svelte';
  import Section from './Section.svelte';
  import { tip } from './tip';
  import {
    countFields,
    errorCounts,
    groupsOf,
    isContainer,
    labelOf,
    shows,
    type Node,
  } from './schema';

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
    /** Children shown as cards of their own. */
    exclude: string[];
    wide: boolean;
  }

  /** A table (array of records, fixed rows, key/value map) or an object of table rows. */
  const isTable = (n: Node) =>
    ['table', 'keyvalue'].includes(n['x-layout'] as string) || Array.isArray(n['x-rows']);

  /** Cards for a tab, all on one grid (#132): one per top-level object; its nested objects
   *  and its tables become cards of their own (titled "Parent · Child"), so no card holds
   *  another card's worth of fields. Tables span the full row; every other card is one grid
   *  cell. Objects spanning several tabs (dfm) split into their children; top-level scalars
   *  (name, family) share a "General" card. */
  const cards = $derived.by((): Card[] => {
    const out: Card[] = [];
    const loose: [string, Node][] = [];
    const card = (node: Node, path: string[], title?: string): void => {
      const exclude: string[] = [];
      const nested: Card[] = [];
      if (isContainer(node) && !Array.isArray(node['x-rows'])) {
        const parent = title ?? labelOf(node, path.at(-1));
        for (const [k, c] of Object.entries(node.properties ?? {})) {
          if (!shows(c, active) || !(isContainer(c) || isTable(c))) continue;
          exclude.push(k);
          const sub = `${parent} · ${labelOf(c, k)}`;
          if (isContainer(c) && !isTable(c)) {
            const before = out.length;
            card(c, [...path, k], sub);
            nested.push(...out.splice(before));
          } else {
            const key = [...path, k].join('.');
            nested.push({ key, path: [...path, k], node: c, title: sub, exclude: [], wide: true });
          }
        }
      }
      const own = isContainer(node)
        ? Object.entries(node.properties ?? {}).some(
            ([k, c]) => !exclude.includes(k) && shows(c, active),
          )
        : true;
      if (own) {
        const key = path.join('.');
        out.push({ key, path, node, title, exclude, wide: !isContainer(node) || isTable(node) });
      }
      out.push(...nested);
    };
    for (const [k, c] of Object.entries(schema.properties ?? {})) {
      if (!shows(c, active)) continue;
      const spans = (c['x-groups-in'] as string[] | undefined)?.length ?? 1;
      if (isContainer(c) && spans > 1) {
        for (const [ck, cc] of Object.entries(c.properties ?? {})) {
          if (shows(cc, active)) card(cc, [k, ck]);
        }
      } else if (!isContainer(c) && !c['x-layout']) loose.push([k, c]);
      else card(c, [k]);
    }
    // Basic cards first, then expert-only ones (collapsed); within each, cells before tables.
    const expertOnly = (c: Card) =>
      isContainer(c.node) &&
      !Object.entries(c.node.properties ?? {}).some(
        ([k, n]) => !c.exclude.includes(k) && countFields(n, active, 'basic'),
      );
    const rank = (c: Card) => 2 * Number(expertOnly(c)) + Number(c.wide);
    out.sort((a, b) => rank(a) - rank(b));
    if (loose.length) {
      const node: Node = { type: 'object', properties: Object.fromEntries(loose) };
      out.unshift({ key: '_general', path: [], node, title: 'General', exclude: [], wide: false });
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
        use:tip={{ text: g.description }}
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
      <Section
        {schema}
        path={c.path}
        node={c.node}
        group={active}
        title={c.title}
        exclude={c.exclude}
        wide={c.wide}
      />
    {/each}
  </div>
</div>
