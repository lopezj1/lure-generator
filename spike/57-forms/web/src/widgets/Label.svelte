<script lang="ts">
  import { theme as basic } from "@sjsf/basic-theme";
  import type { ComponentProps } from "@sjsf/form";
  import Badge from "./Badge.svelte";

  const props: ComponentProps["label"] = $props();
  const Orig = $derived(basic("label", props.config)!);
</script>

<span><Orig {...props} /><Badge config={props.config} /></span>
