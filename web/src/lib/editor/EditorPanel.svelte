<!-- Left panel: design/variant picker, the variant form (validate as you type, save with an
     etag precondition), the build runner and the photo → profile flow.
     API: /api/designs, /api/schema/lure-config, /api/variants/{family}/{name}[/validate],
     /api/spec/status, /api/jobs, /api/uploads. -->
<script lang="ts">
  import '@sjsf/basic-theme/css/pico.css';
  import './editor.css';
  import type { Schema, UiSchemaRoot } from '@sjsf/form';
  import { api } from '../client';
  import type { components } from '../api';
  import { errorText } from '../jobs';
  import ConfigForm from './ConfigForm.svelte';
  import { deepEqual, pointer } from './paths';
  import ProfileFromPhoto from './ProfileFromPhoto.svelte';
  import RunPanel from './RunPanel.svelte';
  import { EditorState } from './state.svelte';

  type S = components['schemas'];
  type FieldError = S['FieldError'];

  let designs: S['DesignSummary'][] = $state([]);
  let family = $state('');
  let variant = $state('');
  let schema = $state<{ schema: Schema; uiSchema: UiSchemaRoot } | null>(null);
  let editor = $state<EditorState | null>(null);
  let form: ConfigForm | undefined = $state();
  let loadError = $state('');
  let generalErrors: FieldError[] = $state([]);
  let validating = $state(false);
  let saving = $state(false);
  let saveMessage = $state('');
  let conflict = $state<{ etag: string; changed: string[] } | null>(null);
  let specStatus: S['SpecStatus'] | null = $state(null);

  const variants = $derived(designs.find((d) => d.family === family)?.variants ?? []);
  const dirty = $derived(editor?.dirty ?? false);

  async function init() {
    const [d, s] = await Promise.all([api.GET('/api/designs'), api.GET('/api/schema/lure-config')]);
    if (!d.data || !s.data) {
      loadError = errorText(d.error ?? s.error, d.response);
      return;
    }
    designs = d.data;
    schema = { schema: s.data.schema as Schema, uiSchema: s.data.uiSchema as UiSchemaRoot };
    family = designs[0]?.family ?? '';
    variant = designs[0]?.variants[0] ?? '';
    refreshStatus();
  }
  init();

  async function refreshStatus() {
    const { data } = await api.GET('/api/spec/status');
    specStatus = data ?? null;
  }

  async function fetchVariant(f: string, v: string) {
    return api.GET('/api/variants/{family}/{name}', { params: { path: { family: f, name: v } } });
  }

  function open(detail: S['VariantDetail']) {
    if (!schema) return;
    editor = new EditorState(detail, schema.schema);
    generalErrors = detail.errors.filter((e) => e.path.length === 0);
    conflict = null;
  }

  async function load(f: string, v: string) {
    loadError = '';
    saveMessage = '';
    editor = null;
    if (!f || !v) return;
    const { data, error, response } = await fetchVariant(f, v);
    if (!data) {
      loadError = errorText(error, response);
      return;
    }
    if (f === family && v === variant) open(data);
  }

  $effect(() => {
    if (schema) load(family, variant);
  });

  function confirmLeave(): boolean {
    return !dirty || confirm('Discard unsaved edits?');
  }

  function pickFamily(e: Event) {
    const el = e.currentTarget as HTMLSelectElement;
    if (!confirmLeave()) return void (el.value = family);
    family = el.value;
    variant = variants[0] ?? '';
  }

  function pickVariant(e: Event) {
    const el = e.currentTarget as HTMLSelectElement;
    if (!confirmLeave()) return void (el.value = variant);
    variant = el.value;
  }

  // Debounced server validation (pydantic model validators + resolve()) of what Save would send.
  $effect(() => {
    if (!editor) return;
    const ed = editor;
    void JSON.stringify(ed.value); // read the whole value: re-run on every edit
    const body = ed.payload();
    const f = family;
    const v = variant;
    const ctrl = new AbortController();
    const timer = setTimeout(async () => {
      validating = true;
      try {
        const { data } = await api.POST('/api/variants/{family}/{name}/validate', {
          params: { path: { family: f, name: v } },
          body,
          signal: ctrl.signal,
        });
        if (data && editor === ed) showErrors(data);
      } catch {
        // aborted by a newer edit
      } finally {
        validating = false;
      }
    }, 400);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  });

  function showErrors(errors: FieldError[]) {
    generalErrors = errors.filter((e) => e.path.length === 0);
    form?.showErrors(errors);
  }

  async function save(force = false) {
    if (!editor || saving) return;
    const ed = editor;
    saving = true;
    saveMessage = '';
    const etag = force && conflict ? conflict.etag : ed.detail.etag;
    try {
      const { data, error, response } = await api.PUT('/api/variants/{family}/{name}', {
        params: { path: { family, name: variant } },
        headers: { 'If-Match': `"${etag}"` },
        body: { config: ed.payload() },
      });
      if (data) {
        saveMessage = data.changed
          ? `Saved ${data.variant.path}${data.backup ? ` (backup ${data.backup})` : ''}`
          : 'No changes to write';
        open(data.variant);
        refreshStatus();
        return;
      }
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      if (response.status === 409) {
        await onConflict((detail as { etag?: string } | undefined)?.etag ?? '');
      } else if (response.status === 422 && Array.isArray(detail)) {
        const errors = detail.filter((d): d is FieldError => Array.isArray(d?.path));
        showErrors(errors);
        saveMessage = `Not saved: ${errors.length || 'some'} error(s)`;
        if (!errors.length) saveMessage += `: ${errorText(error, response)}`;
      } else if (response.status === 428) {
        saveMessage = 'Not saved: the etag was missing. Reload the variant.';
      } else {
        saveMessage = `Not saved: ${errorText(error, response)}`;
      }
    } finally {
      saving = false;
    }
  }

  /** 409: the file changed on disk. Show which keys differ from what we loaded. */
  async function onConflict(etag: string) {
    if (!editor) return;
    const { data } = await fetchVariant(family, variant);
    const changed: string[] = [];
    if (data) {
      const walk = (a: unknown, b: unknown, at: string[]) => {
        const isObj = (x: unknown) => x && typeof x === 'object' && !Array.isArray(x);
        if (isObj(a) && isObj(b)) {
          const ka = a as Record<string, unknown>;
          const kb = b as Record<string, unknown>;
          for (const k of new Set([...Object.keys(ka), ...Object.keys(kb)]))
            walk(ka[k], kb[k], [...at, k]);
        } else if (!deepEqual(a, b)) changed.push(pointer(at) || '/');
      };
      walk(editor.detail.own, data.own, []);
    }
    conflict = { etag: data?.etag ?? etag, changed };
    saveMessage = '';
  }

  async function reload() {
    await load(family, variant);
  }
</script>

<div class="editor">
  <div class="picker grid-2">
    <label>
      Design
      <select value={family} onchange={pickFamily}>
        {#each designs as d (d.family)}
          <option value={d.family}>{d.family}</option>
        {/each}
      </select>
    </label>
    <label>
      Variant
      <select value={variant} onchange={pickVariant}>
        {#each variants as v (v)}
          <option value={v}>{v}</option>
        {/each}
      </select>
    </label>
  </div>

  {#if loadError}
    <p class="error-text">{loadError}</p>
  {/if}

  {#if editor && schema}
    <div class="savebar">
      <small>
        <code>{editor.detail.path}</code>
        {#if specStatus?.dirty}
          <span
            class="badge"
            data-layer="edited"
            title={specStatus.files.map((f) => `${f.status} ${f.path}`).join('\n')}
          >
            spec: {specStatus.files.length} uncommitted
          </span>
        {/if}
        <span class="layers">
          extends {editor.detail.layers
            .slice(1)
            .map((l) => l.label)
            .join(' ← ') || 'nothing'}
        </span>
      </small>
      <div class="savebar-actions">
        <button type="button" disabled={!dirty || saving} aria-busy={saving} onclick={() => save()}>
          Save
        </button>
        <button
          type="button"
          class="secondary outline"
          disabled={!dirty}
          onclick={() => confirmLeave() && reload()}
        >
          Discard
        </button>
        <small aria-busy={validating}>{validating ? 'validating' : ''}</small>
      </div>
      {#if saveMessage}<small>{saveMessage}</small>{/if}
      {#if conflict}
        <article class="conflict">
          <strong>The file changed on disk</strong> since it was loaded.
          <br /><small>
            {conflict.changed.length
              ? `Changed there: ${conflict.changed.join(', ')}`
              : 'Only comments or formatting changed there.'}
          </small>
          <div class="grid-2">
            <button type="button" onclick={reload}>Reload (drop my edits)</button>
            <button type="button" class="secondary" onclick={() => save(true)}>
              Save mine over it
            </button>
          </div>
        </article>
      {/if}
      {#each generalErrors as e (e.message)}
        <small class="error-text">{e.message}</small>
      {/each}
    </div>

    <RunPanel variantPath={editor.detail.path} {dirty} />
    <ProfileFromPhoto {editor} />

    {#key editor}
      <ConfigForm
        bind:this={form}
        {editor}
        schema={schema.schema}
        uiSchema={schema.uiSchema}
        onsubmit={() => save()}
      />
    {/key}
  {:else if !loadError}
    <p aria-busy="true">Loading…</p>
  {/if}
</div>
