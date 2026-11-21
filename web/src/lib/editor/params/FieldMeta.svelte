<!-- Quiet provenance marks next to a label (#121): "variant" when the variant file sets the
     value, "edited" with a revert button, and "reset" back to the inherited value. Inherited
     values get no mark; the label's tooltip names their layer. -->
<script lang="ts">
  import type { Path } from '../paths';
  import { getEditor } from '../state.svelte';
  import type { Node } from './schema';

  let { path, node }: { path: Path; node: Node } = $props();
  const editor = getEditor();

  const origin = $derived(editor.origin(path, node));
  const edited = $derived(editor.isEdited(path));
  const resettable = $derived(editor.canReset(path, node));
</script>

{#if edited}
  <span class="badge" data-layer="edited">edited</span>
  <button
    type="button"
    class="mini"
    title="Revert to the loaded value"
    aria-label="Revert"
    onclick={() => editor.revert(path)}
  >
    ↶
  </button>
{:else if origin === 'variant'}
  <span class="badge" data-layer="variant" title="Set in the variant file">variant</span>
{/if}
{#if resettable}
  <button
    type="button"
    class="mini"
    title="Reset to inherited (parent layers, else default / catalog)"
    aria-label="Reset to inherited"
    onclick={() => editor.reset(path)}
  >
    ↺
  </button>
{/if}
