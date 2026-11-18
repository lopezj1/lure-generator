<!-- textWidget for `x-widget: "path"`: a path relative to the data root, picked from the
     family's profile presets or from uploads, or uploaded here (POST /api/uploads). -->
<script lang="ts">
  import { getFormContext, inputAttributes, type ComponentProps } from '@sjsf/form';
  import { api } from '../../client';
  import { errorText } from '../../jobs';
  import { getEditor } from '../state.svelte';

  let { value = $bindable(), config, handlers }: ComponentProps['textWidget'] = $props();
  const ctx = getFormContext();
  const editor = getEditor();
  const attributes = $derived(inputAttributes(ctx, config, 'text', handlers, {}));
  // profile.preset is a station-table CSV; profile.image / top_image are photos.
  const kind = $derived(String(config.path.at(-1)) === 'preset' ? 'csv' : 'photo');
  const listId = $derived(`${attributes.id ?? config.title}-paths`);

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
      value = json.path;
      handlers.onchange?.();
      await load();
    } catch (err) {
      message = (err as Error).message;
    } finally {
      busy = false;
      input.value = '';
    }
  }
</script>

<div class="path-picker">
  <input
    {...attributes}
    list={listId}
    placeholder={kind === 'csv' ? 'spec/designs/…/profiles/….csv' : 'uploads/….jpg'}
    bind:value={() => value ?? '', (v) => (value = v || undefined)}
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
  <datalist id={listId}>
    {#each options as o (o.path)}
      <option value={o.path}>{o.group}</option>
    {/each}
  </datalist>
</div>
{#if message}<small class="error-text">{message}</small>{/if}
