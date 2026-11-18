<!-- Inherited/overridden badge. Provenance (JSON pointer -> layer) comes from the server. -->
<script lang="ts">
  import type { Config } from "@sjsf/form";
  import { getContext } from "svelte";

  let { config }: { config: Config } = $props();
  const prov = getContext<Record<string, string>>("provenance");
  const s = $derived(config.schema as Record<string, any>);

  const layer = $derived.by(() => {
    if (s.type === "object") return undefined;
    const parts = config.path.map(String);
    // Exact pointer; a list item (numeric last segment) inherits its list's layer.
    const ptr = "/" + parts.join("/");
    if (prov[ptr]) return prov[ptr];
    if (/^\d+$/.test(parts.at(-1) ?? "")) {
      const parent = prov["/" + parts.slice(0, -1).join("/")];
      if (parent) return parent;
    }
    return s["x-derived"] ? "resolved" : "default";
  });
  const label: Record<string, string> = {
    _common: "common", family: "family", default: "default", resolved: "catalog",
  };
</script>

{#if layer}
  <span class="badge" data-layer={label[layer] ? layer : "variant"}>{label[layer] ?? "variant"}</span>
{/if}
