<!-- App shell (#121): a header (variant picker, current build, spec and job status) and
     top-level pages Design | Model | Print | CNC | Builds & jobs, routed by the URL hash.
     Design, Print, CNC and Builds stay mounted while hidden, so unsaved edits, loaded
     toolpaths and job polling survive page switches; Model mounts on demand. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from './lib/client';
  import ModelTab from './lib/model/ModelTab.svelte';
  import EditorPanel from './lib/editor/EditorPanel.svelte';
  import PrintView from './lib/print/PrintView.svelte';
  import CncView from './lib/cnc/CncView.svelte';
  import BuildsPage from './lib/drawer/BuildsPage.svelte';
  import SpecBadge from './lib/drawer/SpecBadge.svelte';
  import { stampTime } from './lib/drawer/format';
  import { runningJobs } from './lib/drawer/state';
  import FabPanel from './lib/fab/FabPanel.svelte';
  import { currentBuild, setCurrentBuild } from './lib/stores/build';
  import { go, route, type Page } from './lib/stores/route';
  import { editorDirty, selection } from './lib/stores/selection';
  import VariantPicker from './lib/ui/VariantPicker.svelte';

  const tabs: { id: Page; label: string }[] = [
    { id: 'design', label: 'Design' },
    { id: 'model', label: 'Model' },
    { id: 'print', label: 'Print' },
    { id: 'cnc', label: 'CNC' },
    { id: 'builds', label: 'Builds & jobs' },
  ];
  const page = $derived($route.page);

  const health = api.GET('/api/health').then(({ data, error }) => {
    if (error || !data) throw new Error('health check failed');
    return data;
  });

  // Picking another variant shows its latest build in the viewers (when it has one).
  $effect(() => {
    const { family, variant } = $selection;
    if (!family || !variant) return;
    untrack(() => {
      const cb = $currentBuild;
      if (cb && cb.family === family && cb.variant === variant) return;
      void api.GET('/api/builds', { params: { query: { family, variant } } }).then(({ data }) => {
        const latest = data?.find((b) => b.latest) ?? data?.[0];
        if (latest) setCurrentBuild(latest.path);
      });
    });
  });

  function onbeforeunload(e: BeforeUnloadEvent) {
    if ($editorDirty) e.preventDefault();
  }
</script>

<svelte:window {onbeforeunload} />

<div class="app">
  <header class="topbar">
    <strong class="brand">Lure generator</strong>
    <VariantPicker />
    {#if $currentBuild}
      <button
        type="button"
        class="current-build"
        title="Current build: {$currentBuild.path}"
        onclick={() => go('builds')}
      >
        <small class="muted">build</small>
        {$currentBuild.stamp === 'latest' ? 'latest' : stampTime($currentBuild.stamp)}
      </button>
    {/if}
    <span class="spacer"></span>
    <SpecBadge />
    <span class="api">
      {#await health}
        <small aria-busy="true">API…</small>
      {:then h}
        <small class="muted" title="API {h.status} · v{h.version} · {h.git_sha}">
          v{h.version} · {h.git_sha}
        </small>
      {:catch err}
        <small class="error-text">API unreachable: {err.message}</small>
      {/await}
    </span>
  </header>

  <nav class="tabs" aria-label="Pages">
    {#each tabs as t (t.id)}
      <a
        href="#/{t.id}"
        class:active={page === t.id}
        aria-current={page === t.id ? 'page' : undefined}
        onclick={(e) => {
          e.preventDefault();
          go(t.id);
        }}
      >
        {t.label}
        {#if t.id === 'design' && $editorDirty}<span class="dot" title="Unsaved edits">●</span>{/if}
        {#if t.id === 'builds' && $runningJobs}
          <span class="badge" data-status="running" aria-busy="true">{$runningJobs}</span>
        {/if}
      </a>
    {/each}
  </nav>

  <main>
    <section class="page page-design" hidden={page !== 'design'}><EditorPanel /></section>
    <section class="page page-viewer page-model" hidden={page !== 'model'}>
      {#if page === 'model'}<ModelTab />{/if}
    </section>
    <section class="page page-viewer page-print" hidden={page !== 'print'}>
      <FabPanel format="print" />
      <!-- The preview row leaves room for the G-code listing side panel (#113). -->
      <div class="preview-row"><PrintView active={page === 'print'} /></div>
    </section>
    <section class="page page-viewer page-cnc" hidden={page !== 'cnc'}>
      <FabPanel format="cnc" />
      <div class="preview-row"><CncView active={page === 'cnc'} /></div>
    </section>
    <section class="page page-builds" hidden={page !== 'builds'}><BuildsPage /></section>
  </main>
</div>
