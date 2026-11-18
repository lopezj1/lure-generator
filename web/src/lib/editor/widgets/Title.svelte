<script lang="ts">
  import { theme as basic } from '@sjsf/basic-theme';
  import type { ComponentProps } from '@sjsf/form';
  import Badge from './Badge.svelte';

  const props: ComponentProps['title'] = $props();
  const Orig = $derived(basic('title', props.config)!);
</script>

<span class="field-label"><Orig {...props} /><Badge config={props.config} /></span>
