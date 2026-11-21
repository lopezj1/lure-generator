<!-- Provides an EditorState to the fields below it (Svelte context is set at init, so the
     parent re-keys this on every newly loaded variant). -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { setEditor, type EditorState } from './state.svelte';

  let { editor, children }: { editor: EditorState; children: Snippet } = $props();
  // svelte-ignore state_referenced_locally
  setEditor(editor);
</script>

{@render children()}
