<!-- Header design/variant picker (#121): GET /api/designs. Asks before dropping unsaved
     edits; the editor, fab bars and builds page follow the `selection` store. -->
<script lang="ts">
  import { api } from '../client';
  import { designs, editorDirty, selection } from '../stores/selection';

  let error = $state('');

  async function init() {
    const { data } = await api.GET('/api/designs');
    if (!data) {
      error = 'could not list designs';
      return;
    }
    designs.set(data);
    if (!$selection.family && data.length) {
      selection.set({ family: data[0].family, variant: data[0].variants[0] ?? '' });
    }
  }
  init();

  const variants = $derived($designs.find((d) => d.family === $selection.family)?.variants ?? []);

  function confirmLeave(): boolean {
    return !$editorDirty || confirm('Discard unsaved edits?');
  }

  function pickFamily(e: Event) {
    const el = e.currentTarget as HTMLSelectElement;
    if (!confirmLeave()) return void (el.value = $selection.family);
    const family = el.value;
    selection.set({
      family,
      variant: $designs.find((d) => d.family === family)?.variants[0] ?? '',
    });
  }

  function pickVariant(e: Event) {
    const el = e.currentTarget as HTMLSelectElement;
    if (!confirmLeave()) return void (el.value = $selection.variant);
    selection.set({ ...$selection, variant: el.value });
  }
</script>

<div class="picker" aria-label="Design and variant">
  {#if error}
    <small class="error-text">{error}</small>
  {:else}
    <select value={$selection.family} onchange={pickFamily} aria-label="Design family">
      {#each $designs as d (d.family)}
        <option value={d.family}>{d.family}</option>
      {/each}
    </select>
    <span class="sep" aria-hidden="true">/</span>
    <select value={$selection.variant} onchange={pickVariant} aria-label="Variant">
      {#each variants as v (v)}
        <option value={v}>{v}</option>
      {/each}
    </select>
  {/if}
</div>

<style>
  .picker {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    min-width: 0;
  }
  select {
    margin: 0;
    width: auto;
    min-width: 0;
    max-width: 16rem;
    padding-top: 0.25rem;
    padding-bottom: 0.25rem;
    font-size: 0.9rem;
    font-weight: 600;
  }
  .sep {
    color: var(--pico-muted-color);
  }
</style>
