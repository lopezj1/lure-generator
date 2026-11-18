<script lang="ts">
  import { api } from './lib/client';
  import ModelTab from './lib/model/ModelTab.svelte';
  import EditorPanel from './lib/editor/EditorPanel.svelte';
  import PrintView from './lib/print/PrintView.svelte';
  import CncView from './lib/cnc/CncView.svelte';
  import BuildsDrawer from './lib/drawer/BuildsDrawer.svelte';
  import SpecBadge from './lib/drawer/SpecBadge.svelte';

  type Tab = 'Model' | 'Print' | 'CNC';
  const tabs: Tab[] = ['Model', 'Print', 'CNC'];
  let tab: Tab = $state('Model');

  const health = api.GET('/api/health').then(({ data, error }) => {
    if (error || !data) throw new Error('health check failed');
    return data;
  });
</script>

<div class="layout">
  <header>
    <nav>
      <ul><li><strong>Lure generator</strong></li></ul>
      <ul>
        <li><SpecBadge /></li>
        <li>
          {#await health}
            <small aria-busy="true">API…</small>
          {:then h}
            <small>API {h.status} · v{h.version} · {h.git_sha}</small>
          {:catch err}
            <small>API unreachable: {err.message}</small>
          {/await}
        </li>
      </ul>
    </nav>
  </header>

  <aside>
    <EditorPanel />
  </aside>

  <main>
    <nav>
      <ul>
        {#each tabs as t (t)}
          <li>
            <button class:outline={tab !== t} onclick={() => (tab = t)}>{t}</button>
          </li>
        {/each}
      </ul>
    </nav>
    <section class="viewport">
      <!-- Kept mounted so a loaded toolpath survives tab switches. -->
      <div class="pane" hidden={tab !== 'Print'}><PrintView active={tab === 'Print'} /></div>
      <div class="pane" hidden={tab !== 'CNC'}><CncView active={tab === 'CNC'} /></div>
      {#if tab === 'Model'}
        <ModelTab />
      {/if}
    </section>
  </main>

  <footer>
    <BuildsDrawer />
  </footer>
</div>
