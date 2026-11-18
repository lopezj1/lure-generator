<!-- textWidget replacement for `x-widget: "material-select"`; options come from x-options
     (server fills them from spec/materials.toml, filtered by category per field). -->
<script lang="ts">
  import { getFormContext, selectAttributes, type ComponentProps } from "@sjsf/form";

  let { value = $bindable(), config, handlers }: ComponentProps["textWidget"] = $props();
  const ctx = getFormContext();
  const s = $derived(config.schema as Record<string, any>);
  const options = $derived((s["x-options"] ?? []) as string[]);
  const { placeholder: _p, ...attributes } = $derived(
    selectAttributes(ctx, config, "select", handlers, {})
  );
</script>

<select {...attributes} bind:value={() => value ?? "", (v) => (value = v || undefined)}>
  {#if s["x-nullable"] || !config.required}
    <option value="">{s["x-derived"] ? "(from catalog)" : "(default)"}</option>
  {/if}
  {#each options as o (o)}
    <option value={o}>{o}</option>
  {/each}
  {#if value && !options.includes(value)}
    <option value={value}>{value} (not in materials.toml)</option>
  {/if}
</select>
