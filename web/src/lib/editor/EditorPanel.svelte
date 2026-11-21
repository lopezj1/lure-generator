<!-- Design page (#121): the selected variant's parameters in group tabs, with a sticky bar to
     save (etag precondition), discard and build. Validates as you type (server-side).
     API: /api/schema/lure-config, /api/variants/{family}/{name}[/validate], /api/spec/status,
     /api/jobs, /api/uploads. The variant comes from the header picker (`selection`). -->
<script lang="ts">
  import './editor.css';
  import './params/params.css';
  import { api } from '../client';
  import type { components } from '../api';
  import { errorText } from '../jobs';
  import { route, setSub } from '../stores/route';
  import { editorDirty, selection } from '../stores/selection';
  import ParamEditor from './params/ParamEditor.svelte';
  import type { Node } from './params/schema';
  import { deepEqual, pointer } from './paths';
  import ProfileFromPhoto from './ProfileFromPhoto.svelte';
  import RunPanel from './RunPanel.svelte';
  import EditorScope from './EditorScope.svelte';
  import { EditorState } from './state.svelte';

  type S = components['schemas'];
  type FieldError = S['FieldError'];

  let schema = $state.raw<Node | null>(null);
  let editor = $state<EditorState | null>(null);
  let loadError = $state('');
  let generalErrors: FieldError[] = $state([]);
  let validating = $state(false);
  let saving = $state(false);
  let saveMessage = $state('');
  let conflict = $state<{ etag: string; changed: string[] } | null>(null);
  let specStatus: S['SpecStatus'] | null = $state(null);

  const family = $derived($selection.family);
  const variant = $derived($selection.variant);
  const dirty = $derived(editor?.dirty ?? false);
  const group = $derived($route.page === 'design' ? $route.sub : '');
  $effect(() => editorDirty.set(dirty));

  async function init() {
    const { data, error, response } = await api.GET('/api/schema/lure-config');
    if (!data) {
      loadError = errorText(error, response);
      return;
    }
    schema = data.schema as Node;
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
    editor = new EditorState(detail, schema);
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
    if (editor) editor.errors = errors.filter((e) => e.path.length > 0);
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

  function onkey(e: KeyboardEvent) {
    if ($route.page !== 'design' || !(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 's')
      return;
    e.preventDefault();
    if (dirty) void save();
  }

  const errorCount = $derived((editor?.errors.length ?? 0) + generalErrors.length);
</script>

<svelte:window onkeydown={onkey} />

<div class="editor">
  {#if loadError}
    <p class="error-text">{loadError}</p>
  {/if}

  {#if editor && schema}
    {#key editor}
      <EditorScope {editor}>
        <div class="savebar">
          <div class="savebar-info">
            <code
              title="extends {editor.detail.layers
                .slice(1)
                .map((l) => l.path)
                .join(' ← ')}"
            >
              {editor.detail.path}
            </code>
            <small class="muted">
              extends {editor.detail.layers
                .slice(1)
                .map((l) => l.label)
                .join(' ← ') || 'nothing'}
            </small>
            {#if specStatus?.dirty}
              <span
                class="badge"
                data-layer="edited"
                title={specStatus.files.map((f) => `${f.status} ${f.path}`).join('\n')}
              >
                spec: {specStatus.files.length} uncommitted
              </span>
            {/if}
          </div>
          <div class="savebar-actions">
            <small class="state" aria-live="polite" aria-busy={validating}>
              {#if validating}validating{:else if errorCount}<span class="error-text">
                  {errorCount} error(s)
                </span>{:else if dirty}unsaved edits{:else}saved{/if}
            </small>
            <button
              type="button"
              class="secondary outline"
              disabled={!dirty}
              onclick={() => confirm('Discard unsaved edits?') && reload()}
            >
              Discard
            </button>
            <button
              type="button"
              disabled={!dirty || saving}
              aria-busy={saving}
              title="Save (Ctrl+S)"
              onclick={() => save()}
            >
              Save
            </button>
            <RunPanel variantPath={editor.detail.path} {dirty} />
          </div>
          {#if saveMessage}<small class="message">{saveMessage}</small>{/if}
          {#if conflict}
            <article class="conflict">
              <strong>The file changed on disk</strong> since it was loaded.
              <br /><small>
                {conflict.changed.length
                  ? `Changed there: ${conflict.changed.join(', ')}`
                  : 'Only comments or formatting changed there.'}
              </small>
              <div class="conflict-actions">
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

        <ParamEditor {schema} {group} onpick={(g) => setSub('design', g)}>
          {#snippet extras(g)}
            {#if g === 'shape'}<ProfileFromPhoto editor={editor!} />{/if}
          {/snippet}
        </ParamEditor>
      </EditorScope>
    {/key}
  {:else if !loadError}
    <p aria-busy="true">Loading…</p>
  {/if}
</div>
