<!-- A path relative to the data root (`x-widget: "path"`), picked from the family's profile
     presets or from uploads, or uploaded here (POST /api/uploads). -->
<script lang="ts">
  import { api } from '../../client';
  import { errorText } from '../../jobs';
  import { getEditor } from '../state.svelte';

  let {
    id,
    value,
    kind,
    invalid,
    label,
    onchange,
  }: {
    id: string;
    value: string | undefined;
    kind: 'csv' | 'photo';
    invalid: boolean;
    label: string;
    onchange: (v: string | undefined) => void;
  } = $props();
  const editor = getEditor();

  let options: { group: string; path: string }[] = $state([]);
  let busy = $state(false);
  let message = $state('');

  async function load() {
    const found: { group: string; path: string }[] = [];
    if (kind === 'csv') {
      const { data } = await api.GET('/api/designs/{family}/presets', {
        params: { path: { family: editor.family } },
      });
      for (const p of data ?? []) found.push({ group: 'preset', path: p });
    }
    const { data } = await api.GET('/api/uploads', { params: { query: { kind } } });
    for (const u of data ?? []) found.push({ group: 'upload', path: u.path });
    options = found;
  }
  $effect(() => {
    void kind;
    load();
  });

  async function upload(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    busy = true;
    message = '';
    const body = new FormData();
    body.append('file', file);
    try {
      const res = await fetch(`/api/uploads?kind=${kind}`, { method: 'POST', body });
      const json = await res.json();
      if (!res.ok) throw new Error(errorText(json, res));
      onchange(json.path);
      await load();
    } catch (err) {
      message = (err as Error).message;
    } finally {
      busy = false;
      input.value = '';
    }
  }
</script>

<div class="path-field">
  <input
    {id}
    list="{id}-paths"
    aria-label={label}
    aria-invalid={invalid || undefined}
    placeholder={kind === 'csv' ? 'spec/designs/…/profiles/….csv' : 'uploads/….jpg'}
    value={value ?? ''}
    onchange={(e) => onchange(e.currentTarget.value || undefined)}
  />
  <label class="mini" aria-busy={busy} title="Upload a {kind === 'csv' ? 'station CSV' : 'photo'}">
    upload
    <input
      type="file"
      hidden
      accept={kind === 'csv' ? '.csv,text/csv' : 'image/*'}
      onchange={upload}
    />
  </label>
  <datalist id="{id}-paths">
    {#each options as o (o.path)}
      <option value={o.path}>{o.group}</option>
    {/each}
  </datalist>
</div>
{#if message}<small class="error-text">{message}</small>{/if}

<style>
  .path-field {
    display: flex;
    gap: 0.4rem;
    align-items: center;
    min-width: 0;
  }
  .path-field input {
    margin: 0;
    min-width: 0;
  }
</style>
