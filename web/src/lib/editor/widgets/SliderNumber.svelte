<!-- numberWidget for `x-widget: "slider"`: a range and a number box sharing one value. -->
<script lang="ts">
  import { getFormContext, inputAttributes, type ComponentProps } from '@sjsf/form';

  let { value = $bindable(), config, handlers }: ComponentProps['numberWidget'] = $props();
  const ctx = getFormContext();
  const s = $derived(config.schema as Record<string, number | boolean | undefined>);
  const attributes = $derived(
    inputAttributes(ctx, config, 'number', handlers, {
      type: 'number',
      step: (s['x-step'] as number | undefined) ?? 'any',
    }),
  );
</script>

<div class="slider-number">
  <input
    type="range"
    min={s.minimum as number}
    max={s.maximum as number}
    step={s['x-step'] as number}
    aria-label="{config.title} slider"
    disabled={attributes.disabled}
    bind:value={() => value ?? (s.minimum as number), (v) => (value = v)}
    oninput={handlers.oninput}
    onchange={handlers.onchange}
  />
  <input
    {...attributes}
    placeholder={s['x-nullable'] ? 'unset' : undefined}
    bind:value={() => value ?? null, (v) => (value = v ?? undefined)}
  />
</div>
