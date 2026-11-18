<!-- tupleField replacement for `x-widget: "range-pair"`: [lo, hi] as two number boxes. -->
<script lang="ts">
  import {
    getComponent,
    getFormContext,
    getFieldsErrors,
    getSubtreePaths,
    getIdByPath,
    makeEventHandlers,
    validateField,
    type ComponentProps,
  } from "@sjsf/form";

  let { config, value = $bindable(), uiOption }: ComponentProps["tupleField"] = $props();
  const ctx = getFormContext();
  const Template = $derived(getComponent(ctx, "fieldTemplate", config));
  const handlers = makeEventHandlers(ctx, () => config, () => validateField(ctx, config, value));
  const errors = $derived(getFieldsErrors(ctx, getSubtreePaths(ctx, config.path)));
  const id = $derived(getIdByPath(ctx, config.path));
  const s = $derived(config.schema as Record<string, any>);

  function get(i: number) {
    return (value?.[i] as number | undefined) ?? null;
  }
  function set(i: number, v: number | null) {
    const next = [value?.[0], value?.[1]];
    next[i] = v ?? undefined;
    // A nullable pair cleared on both ends means "unset" (catalog value).
    value = next.every((x) => x === undefined) ? undefined : (next as number[]);
  }
  // sjsf pre-fills a new array item's fixed tuple with [null, null]; for an optional
  // pair that means "unset", so normalise it away (else Ajv: "must be number").
  $effect(() => {
    if (s["x-nullable"] && Array.isArray(value) && value.every((x) => x == null)) {
      value = undefined;
    }
  });
  const misordered = $derived(get(0) !== null && get(1) !== null && get(0)! > get(1)!);
</script>

<Template
  type="template"
  showTitle
  useLabel
  widgetType="numberWidget"
  {uiOption}
  {value}
  {config}
  errors={misordered ? [...errors, "min must be ≤ max"] : errors}
>
  <div class="range-pair">
    <input
      type="number"
      {id}
      aria-label="{config.title} min"
      aria-invalid={misordered || errors.length > 0 || undefined}
      placeholder={s["x-nullable"] ? "unset" : "min"}
      bind:value={() => get(0), (v) => set(0, v)}
      {...handlers}
    />
    <span>to</span>
    <input
      type="number"
      aria-label="{config.title} max"
      aria-invalid={misordered || errors.length > 0 || undefined}
      placeholder={s["x-nullable"] ? "unset" : "max"}
      bind:value={() => get(1), (v) => set(1, v)}
      {...handlers}
    />
  </div>
</Template>
