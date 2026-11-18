<!-- Where a field's value comes from (variant / family / common / default / catalog), whether
     it was edited since loading, and the reset actions. Provenance comes from the server. -->
<script lang="ts">
  import type { Config } from '@sjsf/form';
  import { getEditor } from '../state.svelte';

  let { config }: { config: Config } = $props();
  const editor = getEditor();
  const s = $derived(config.schema as Record<string, unknown>);
  const path = $derived(config.path as ReadonlyArray<string | number>);
  const show = $derived(s.type !== 'object' && path.length > 0);
  const origin = $derived(editor.origin(path, s));
  const edited = $derived(editor.isEdited(path));
  const resettable = $derived(editor.canReset(path, s));
</script>

{#if show}
  <span class="badge" data-layer={origin}>{origin}</span>
  {#if edited}
    <span class="badge" data-layer="edited">edited</span>
    <button
      type="button"
      class="mini"
      title="Revert to the loaded value"
      onclick={() => editor.revert(path)}
    >
      ↶
    </button>
  {/if}
  {#if resettable}
    <button
      type="button"
      class="mini"
      title="Reset to inherited (parent layers, else default / catalog)"
      onclick={() => editor.reset(path)}
    >
      reset
    </button>
  {/if}
{/if}
