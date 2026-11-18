<!-- tupleField for `x-widget: "range-pair"`: [lo, hi] as two number boxes, checking lo ≤ hi. -->
<script lang="ts">
  import {
    getComponent,
    getFieldsErrors,
    getFormContext,
    getIdByPath,
    getSubtreePaths,
    makeEventHandlers,
    validateField,
    type ComponentProps,
  } from '@sjsf/form';

  let { config, value = $bindable(), uiOption }: ComponentProps['tupleField'] = $props();
  const ctx = getFormContext();
  const Template = $derived(getComponent(ctx, 'fieldTemplate', config));
  const handlers = makeEventHandlers(
    ctx,
    () => config,
    () => validateField(ctx, config, value),
  );
  const errors = $derived(getFieldsErrors(ctx, getSubtreePaths(ctx, config.path)));
  const id = $derived(getIdByPath(ctx, config.path));
  const nullable = $derived(Boolean((config.schema as Record<string, unknown>)['x-nullable']));

  function get(i: number): number | null {
    const v = value?.[i];
    return typeof v === 'number' ? v : null;
  }
  function set(i: number, v: number | null) {
    const next: (number | undefined)[] = [get(0) ?? undefined, get(1) ?? undefined];
    next[i] = v ?? undefined;
    // A nullable pair cleared on both ends means "unset" (catalog / default value).
    value = next.every((x) => x === undefined) ? undefined : (next as number[]);
  }
  // sjsf pre-fills a new array item's fixed tuple with [null, null]; for an optional pair that
  // means "unset", so normalise it away (else Ajv: "must be number").
  $effect(() => {
    if (nullable && Array.isArray(value) && value.every((x) => x == null)) value = undefined;
  });
  const lo = $derived(get(0));
  const hi = $derived(get(1));
  const misordered = $derived(lo !== null && hi !== null && lo > hi);
</script>

<Template
  type="template"
  showTitle
  useLabel
  widgetType="numberWidget"
  {uiOption}
  {value}
  {config}
  errors={misordered ? [...errors, 'min must be ≤ max'] : errors}
>
  <div class="range-pair">
    <input
      type="number"
      step="any"
      {id}
      aria-label="{config.title} min"
      aria-invalid={misordered || errors.length > 0 || undefined}
      placeholder={nullable ? 'unset' : 'min'}
      bind:value={() => lo, (v) => set(0, v)}
      {...handlers}
    />
    <span>to</span>
    <input
      type="number"
      step="any"
      aria-label="{config.title} max"
      aria-invalid={misordered || errors.length > 0 || undefined}
      placeholder={nullable ? 'unset' : 'max'}
      bind:value={() => hi, (v) => set(1, v)}
      {...handlers}
    />
  </div>
</Template>
