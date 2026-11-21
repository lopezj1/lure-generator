<script lang="ts">
  // Model tab: build picker, three-cad-viewer (lazy), section panel, report overlay.
  // Follows the shared `currentBuild` store (set by the editor's Build action) and writes it
  // back when the user picks another build here.
  import { api } from '../client';
  import { currentBuild, setCurrentBuild } from '../stores/build';
  import FullscreenButton from '../ui/FullscreenButton.svelte';
  import CadView from './CadView.svelte';
  import ReportOverlay from './ReportOverlay.svelte';
  import SectionPanel from './SectionPanel.svelte';
  import { AXES, buildBase, hasModel, type Axis, type BuildSummary, type ModelInfo } from './types';

  let builds: BuildSummary[] = $state([]);
  let selected = $state('');
  let deviation = $state(0.1);
  let info: ModelInfo | null = $state.raw(null);
  let shapes: unknown = $state.raw(null);
  let error = $state('');
  let status = $state('');
  let loading = $state(false);
  let cad: CadView | null = $state(null);
  let rendered = $state(0); // bumps on every viewer render
  let viewerEl: HTMLDivElement | undefined = $state();

  let axis: Axis = $state('x');
  let offset = $state(0);
  let sync = $state(true);

  const key = (b: BuildSummary) => `${b.family}/${b.variant}/${b.stamp}`;
  const build = $derived(builds.find((b) => key(b) === selected) ?? null);
  const base = $derived(build ? buildBase(build) : '');

  async function loadBuilds() {
    const { data, error: e } = await api.GET('/api/builds');
    if (e || !data) {
      error = 'could not list builds';
      return;
    }
    builds = data.filter(hasModel);
    if (!builds.some((b) => key(b) === selected)) {
      selected = builds.find((b) => b.latest) ? key(builds.find((b) => b.latest)!) : '';
      if (!selected && builds.length) selected = key(builds[0]);
    }
  }
  loadBuilds();

  // A new current build (e.g. a finished build job): refresh the list and select it.
  $effect(() => {
    const cb = $currentBuild;
    if (!cb) return;
    const want = `${cb.family}/${cb.variant}/${cb.stamp}`;
    if (want === selected) return;
    loadBuilds().then(() => {
      if (builds.some((b) => key(b) === want)) selected = want;
    });
  });

  function pick(value: string) {
    selected = value;
    if (value) setCurrentBuild(`builds/${value}`);
  }

  // Parts and bounds, then the (cached) tessellation.
  $effect(() => {
    if (!base) return;
    const dev = deviation;
    const ctrl = new AbortController();
    loading = true;
    error = '';
    info = null;
    shapes = null;
    (async () => {
      try {
        const r = await fetch(`${base}/model`, { signal: ctrl.signal });
        if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText);
        const m = (await r.json()) as ModelInfo;
        info = m;
        if (m.bbox_min && m.bbox_max) {
          const i = AXES.indexOf(axis);
          offset = Math.round(((m.bbox_min[i] + m.bbox_max[i]) / 2) * 10) / 10;
        }
        const t0 = performance.now();
        const t = await fetch(`${base}/model/tessellation?deviation=${dev}&angular=0.2`, {
          signal: ctrl.signal,
        });
        if (!t.ok) throw new Error((await t.json()).detail ?? t.statusText);
        shapes = await t.json();
        const cache = t.headers.get('x-luregen-cache') ?? '?';
        status = `tessellation ${(performance.now() - t0).toFixed(0)} ms (cache ${cache})`;
      } catch (e) {
        if (!ctrl.signal.aborted) error = e instanceof Error ? e.message : String(e);
      } finally {
        if (!ctrl.signal.aborted) loading = false;
      }
    })();
    return () => ctrl.abort();
  });

  // Section plane → viewer clip plane (one plane, the others moved out of the way).
  $effect(() => {
    if (!cad || !shapes || !rendered) return;
    if (sync) cad.clip(AXES.indexOf(axis) as 0 | 1 | 2, offset);
    else cad.clip(null);
  });

  // Viewer clip slider → section offset (only for the section's axis).
  function onclip(i: number, world: number) {
    if (sync && AXES[i] === axis) offset = Math.round(world * 10) / 10;
  }

  function onrendered(r: { triangles: number; ms: number; version: string }) {
    rendered += 1;
    status = `${status.split(' · render')[0]} · render ${r.ms.toFixed(0)} ms · ${r.triangles.toLocaleString()} triangles · three-cad-viewer ${r.version}`;
  }

  const PRESETS = ['iso', 'front', 'top', 'right', 'rear'] as const;
</script>

<div class="model-tab">
  <div class="bar">
    <label>
      Build
      <select
        value={selected}
        onchange={(e) => pick(e.currentTarget.value)}
        disabled={!builds.length}
      >
        {#each builds as b (key(b))}
          <option value={key(b)}>
            {b.family}/{b.variant} · {b.stamp}{b.latest ? ' (latest)' : ''}
          </option>
        {/each}
      </select>
    </label>
    <button class="outline secondary" onclick={loadBuilds} title="Reload the build list">↻</button>
    <label>
      Deviation
      <select bind:value={deviation}>
        {#each [0.05, 0.1, 0.2, 0.5] as d (d)}<option value={d}>{d} mm</option>{/each}
      </select>
    </label>
    <div role="group" class="presets">
      {#each PRESETS as p (p)}
        <button class="outline secondary" onclick={() => cad?.preset(p)}>{p}</button>
      {/each}
    </div>
    <small class="status" aria-busy={loading}>{error || status}</small>
  </div>

  {#if !builds.length}
    <p>
      <small>No builds with a model yet. Run <code>lure build &lt;variant&gt;</code> first.</small>
    </p>
  {:else}
    <div class="body">
      <div class="viewer" bind:this={viewerEl}>
        <FullscreenButton target={viewerEl} />
        {#if shapes}
          <CadView bind:this={cad} data={shapes} {onclip} {onrendered} />
        {/if}
        {#if base}<ReportOverlay {base} />{/if}
      </div>
      {#if info && base}
        <div class="side">
          <SectionPanel {base} {info} bind:axis bind:offset bind:sync />
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .model-tab {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
  }
  .bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 0.75rem;
    align-items: center;
    padding: 0 0 0.4rem;
    font-size: 0.8rem;
  }
  .bar label {
    min-width: 0;
    max-width: 100%;
    display: flex;
    gap: 0.4rem;
    align-items: center;
    margin: 0;
  }
  .bar select,
  .bar button {
    margin: 0;
    padding: 0.2rem 0.5rem;
    font-size: 0.8rem;
    width: auto;
    min-width: 0;
    max-width: 100%;
    height: auto;
  }
  .bar label:first-child {
    flex: 0 1 32rem;
  }
  .bar label:first-child select {
    flex: 1;
    width: 100%;
  }
  .presets {
    width: auto;
    margin: 0;
  }
  .status {
    margin-left: auto;
    opacity: 0.75;
  }
  .body {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(16rem, 22rem);
    background: var(--pico-card-background-color);
    border-radius: var(--pico-border-radius);
  }
  .viewer {
    position: relative;
    min-width: 0;
    min-height: 24rem;
    overflow: hidden;
  }
  /* Clear of the viewer's own toolbar, tree and view menu: bottom centre. */
  .viewer :global(.fs-btn) {
    top: auto;
    right: auto;
    bottom: 0.5rem;
    left: 50%;
    transform: translateX(-50%);
  }
  @media (max-width: 767px) {
    /* The report overlay takes the bottom right; keep the bottom left for this. */
    .viewer :global(.fs-btn) {
      left: 0.5rem;
      transform: none;
    }
  }
  .side {
    overflow: auto;
    border-left: 1px solid var(--pico-muted-border-color);
  }
  @media (max-width: 1023px) {
    .body {
      grid-template-columns: minmax(0, 1fr);
    }
    .viewer {
      height: max(22rem, calc(100dvh - 10rem));
    }
    .side {
      border-left: 0;
      border-top: 1px solid var(--pico-muted-border-color);
    }
  }
</style>
