<!-- One config value as a labeled control (#121), or a bare control in a table cell
     (`compact`). The control follows the schema: enums and material lists → select,
     bounded numbers → slider + number (forms only), [min, max] pairs → two numbers,
     string lists → comma-separated text, booleans → switch, paths → picker with upload.
     Empty means "unset" for optional fields (the default / parent layers apply). -->
<script lang="ts">
  import { pointer, type Path } from '../paths';
  import { getEditor } from '../state.svelte';
  import FieldMeta from './FieldMeta.svelte';
  import PathField from './PathField.svelte';
  import { tip } from './tip';
  import { errorsAt, labelOf, unitOf, type Node } from './schema';

  let {
    path,
    node,
    compact = false,
    required = false,
    label: labelOverride,
  }: {
    path: Path;
    node: Node;
    compact?: boolean;
    required?: boolean;
    label?: string;
  } = $props();

  const editor = getEditor();
  const key = $derived(path[path.length - 1] ?? '');
  const label = $derived(labelOverride ?? labelOf(node, key));
  const unit = $derived(unitOf(node));
  const id = $derived('f' + pointer(path).replace(/[^\w-]/g, '_'));
  const value = $derived(editor.get(path));
  const errors = $derived(errorsAt(editor.errors, path));
  const invalid = $derived(errors.length > 0);
  const edited = $derived(editor.isEdited(path));
  const optional = $derived(Boolean(node['x-nullable']) || !required);
  const widget = $derived(node['x-widget'] as string | undefined);
  const derivedKind = $derived(node['x-derived'] as string | undefined);
  const type = $derived(node.type as string | undefined);
  const tooltip = $derived(
    [node.description, `from: ${editor.originText(path, node)}`].filter(Boolean).join('\n'),
  );
  const placeholder = $derived(
    derivedKind === 'catalog'
      ? 'catalog'
      : node.default != null
        ? String(node.default)
        : optional
          ? 'auto'
          : '',
  );
  const options = $derived.by(() => {
    if (Array.isArray(node.enum)) return node.enum.map(String);
    if (Array.isArray(node['x-options'])) return node['x-options'] as string[];
    return null;
  });
  const suggest = $derived(
    Array.isArray(node['x-suggest'])
      ? (node['x-suggest'] as { value: string; label: string }[])
      : null,
  );
  const step = $derived(
    typeof node['x-step'] === 'number' ? node['x-step'] : type === 'integer' ? 1 : 'any',
  );
  const min = $derived(node.minimum ?? node.exclusiveMinimum);
  const max = $derived(node.maximum ?? node.exclusiveMaximum);

  function set(v: unknown) {
    editor.set(path, v);
  }
  const num = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? undefined : v);

  // [min, max] pairs: a nullable pair cleared on both ends is unset.
  const pair = $derived(Array.isArray(value) ? (value as (number | null)[]) : [null, null]);
  function setPair(i: number, v: number | null) {
    const next = [pair[0] ?? undefined, pair[1] ?? undefined];
    next[i] = v ?? undefined;
    set(next.every((x) => x === undefined) && optional ? undefined : next);
  }
  const misordered = $derived(
    widget === 'range-pair' && pair[0] != null && pair[1] != null && pair[0] > pair[1],
  );

  const listText = $derived(Array.isArray(value) ? (value as unknown[]).join(', ') : '');
  function setList(text: string) {
    const items = text
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    set(items);
  }
</script>

{#snippet control()}
  {#if node.readOnly}
    <output {id} class="readonly" title="Computed by the build">
      {value ?? '—'}{unit && value != null ? ` ${unit}` : ''}
    </output>
  {:else if widget === 'range-pair'}
    <div class="pair">
      <input
        {id}
        type="number"
        step="any"
        aria-label="{label} min"
        aria-invalid={invalid || misordered || undefined}
        placeholder={optional ? placeholder || 'min' : 'min'}
        bind:value={() => pair[0] ?? null, (v) => setPair(0, v)}
      />
      <span>–</span>
      <input
        type="number"
        step="any"
        aria-label="{label} max"
        aria-invalid={invalid || misordered || undefined}
        placeholder={optional ? placeholder || 'max' : 'max'}
        bind:value={() => pair[1] ?? null, (v) => setPair(1, v)}
      />
      {#if unit && !compact}<span class="unit">{unit}</span>{/if}
    </div>
  {:else if type === 'array'}
    <input
      {id}
      aria-label={label}
      aria-invalid={invalid || undefined}
      placeholder="a, b, c"
      value={listText}
      onchange={(e) => setList(e.currentTarget.value)}
    />
  {:else if options}
    <select
      {id}
      aria-label={label}
      aria-invalid={invalid || undefined}
      bind:value={() => (value == null ? '' : String(value)), (v) => set(v === '' ? undefined : v)}
    >
      {#if optional || value == null}
        <option value="">{derivedKind === 'catalog' ? '(catalog)' : '(default)'}</option>
      {/if}
      {#each options as o (o)}
        <option value={o}>{o}</option>
      {/each}
      {#if value != null && !options.includes(String(value))}
        <option value={String(value)}>{value} (unknown)</option>
      {/if}
    </select>
  {:else if widget === 'path'}
    <PathField
      {id}
      {label}
      {invalid}
      value={value as string | undefined}
      kind={String(key) === 'preset' ? 'csv' : 'photo'}
      onchange={set}
    />
  {:else if type === 'boolean' && node['x-nullable']}
    <select
      {id}
      aria-label={label}
      bind:value={
        () => (value == null ? '' : String(value)), (v) => set(v === '' ? undefined : v === 'true')
      }
    >
      <option value="">{derivedKind === 'catalog' ? '(catalog)' : '(default)'}</option>
      <option value="true">yes</option>
      <option value="false">no</option>
    </select>
  {:else if type === 'boolean'}
    <input
      {id}
      type="checkbox"
      role="switch"
      aria-label={label}
      bind:checked={() => Boolean(value), (v) => set(v)}
    />
  {:else if type === 'number' || type === 'integer'}
    <div class="num" class:slider={widget === 'slider' && !compact}>
      {#if widget === 'slider' && !compact}
        <input
          type="range"
          {min}
          {max}
          {step}
          tabindex="-1"
          aria-hidden="true"
          bind:value={() => (value as number) ?? min ?? 0, (v) => set(v)}
        />
      {/if}
      <input
        {id}
        type="number"
        {min}
        {max}
        {step}
        aria-label={label}
        aria-invalid={invalid || undefined}
        {placeholder}
        bind:value={() => (value as number | null) ?? null, (v) => set(num(v))}
      />
      {#if unit && !compact}<span class="unit">{unit}</span>{/if}
    </div>
  {:else}
    <input
      {id}
      list={suggest ? `${id}-suggest` : undefined}
      aria-label={label}
      aria-invalid={invalid || undefined}
      {placeholder}
      value={(value as string) ?? ''}
      oninput={(e) =>
        set(e.currentTarget.value === '' && optional ? undefined : e.currentTarget.value)}
    />
    {#if suggest}
      <datalist id="{id}-suggest">
        {#each suggest as s (s.value)}<option value={s.value}>{s.label}</option>{/each}
      </datalist>
    {/if}
  {/if}
{/snippet}

{#if compact}
  <div class="cell" class:edited title={[tooltip, ...errors].join('\n')}>
    {@render control()}
    {#each errors as e (e)}<small class="error-text">{e}</small>{/each}
    {#if misordered}<small class="error-text">min must be ≤ max</small>{/if}
  </div>
{:else}
  <div class="field" class:edited class:invalid>
    <label
      for={id}
      use:tip={{
        text: node.description,
        sub: `from: ${editor.originText(path, node)}`,
        focusWithin: '.field',
      }}
    >
      <span class="name">{label}</span>
      <FieldMeta {path} {node} />
    </label>
    <div class="control">{@render control()}</div>
    {#if errors.length || misordered}
      <div class="errors">
        {#each errors as e (e)}<small class="error-text">{e}</small>{/each}
        {#if misordered}<small class="error-text">min must be ≤ max</small>{/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .field {
    display: grid;
    grid-template-columns: minmax(7.5rem, 2fr) minmax(8rem, 3fr);
    gap: 0.15rem 0.75rem;
    align-items: center;
    padding: 0.2rem 0;
  }
  .field label {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.15rem 0.35rem;
    align-items: baseline;
    font-size: 0.85rem;
    cursor: help;
  }
  .field.edited .name {
    color: var(--pico-primary);
  }
  .control {
    min-width: 0;
  }
  .errors {
    grid-column: 2;
  }
  .num,
  .pair {
    display: flex;
    gap: 0.4rem;
    align-items: center;
    min-width: 0;
  }
  .num.slider input[type='range'] {
    flex: 3;
    min-width: 3rem;
  }
  .num.slider input[type='number'] {
    flex: 2;
    min-width: 4.5rem;
  }
  .pair input {
    min-width: 3.5rem;
  }
  .unit,
  .pair span {
    color: var(--pico-muted-color);
    font-size: 0.8rem;
    white-space: nowrap;
  }
  .readonly {
    font-size: 0.85rem;
    color: var(--pico-muted-color);
  }
  input,
  select {
    margin: 0;
  }
  input[type='checkbox'] {
    margin: 0;
  }
  .cell {
    min-width: 0;
  }
  .cell.edited :global(input),
  .cell.edited :global(select) {
    border-color: var(--pico-primary);
  }
  .cell small {
    font-size: 0.7rem;
    white-space: normal;
  }
  @media (max-width: 599px) {
    .field {
      grid-template-columns: 1fr;
    }
    .errors {
      grid-column: 1;
    }
  }
</style>
