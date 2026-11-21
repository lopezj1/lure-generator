<script lang="ts">
  /** Print G-code preview (W6, #67): layer/move scrubbing, legend, markers and stats. */
  import { onDestroy, onMount } from 'svelte';
  import { api } from '../client';
  import { currentBuild } from '../stores/build';
  import { fetchToolpath, type PrintToolpath } from '../toolpath/reader';
  import { drawRange, formatDuration, layerTicks, primeTowerFeature } from './model';
  import { PAUSE_COLOR, TRAVEL_COLOR, cssColor, featureColor, toolColor } from './palette';
  import FullscreenButton from '../ui/FullscreenButton.svelte';
  import RangeSlider, { type Tick } from './RangeSlider.svelte';
  import { PrintScene, type ColorMode } from './scene';

  let { active = true }: { active?: boolean } = $props();

  interface Source {
    label: string;
    url: string;
  }

  let host: HTMLDivElement | undefined = $state();
  let stage: HTMLDivElement | undefined = $state();
  let scene = $state.raw<PrintScene | null>(null);
  let sources: Source[] = $state([]);
  let selected = $state('');
  let loadedUrl = $state('');
  let status = $state('');
  let error = $state('');
  let tp = $state.raw<PrintToolpath | null>(null);
  let timing = $state({ fetch: 0, build: 0 });

  let lo = $state(0);
  let hi = $state(0);
  let moves = $state(0);
  let colorMode: ColorMode = $state('feature');
  let showTravel = $state(false);
  let showPause = $state(true);
  let hidden: boolean[] = $state([]);
  let statsOpen = $state(true);

  const header = $derived(tp?.header);
  const sourceLabel = $derived(sources.find((s) => s.url === loadedUrl)?.label ?? '');
  const offsets = $derived(header?.layer_offsets ?? [0, 0]);
  const layerMax = $derived(Math.max(0, offsets.length - 2));
  const layerLen = (l: number) => offsets[l + 1] - offsets[l];
  const range = $derived(drawRange(offsets, lo, hi, moves));
  const prime = $derived(primeTowerFeature(header?.features ?? []));
  const ticks = $derived(tp ? layerTicks(tp) : []);
  const pauses = $derived(header?.markers.filter((m) => m.type === 'pause') ?? []);
  const toolChanges = $derived(header?.markers.filter((m) => m.type === 'tool_change') ?? []);

  const layerTickMarks: Tick[] = $derived(
    ticks.map((t) => ({
      value: t.layer,
      title: `Layer ${t.layer}: ${t.type === 'pause' ? 'pause ' : 'tool change '}${t.title}`,
      color:
        t.type === 'pause' ? PAUSE_COLOR : cssColor(toolColor(header?.tools[t.tool ?? 0]?.color)),
      wide: t.type === 'pause',
    })),
  );
  const moveTickMarks: Tick[] = $derived(
    (header?.markers ?? [])
      .filter((m) => m.layer === hi)
      .map((m) => ({
        value: m.segment - offsets[hi],
        title: m.type === 'pause' ? `${m.command} (line ${m.line})` : `tool change → T${m.tool}`,
        color: m.type === 'pause' ? PAUSE_COLOR : cssColor(toolColor(header?.tools[m.tool]?.color)),
        wide: m.type === 'pause',
      })),
  );

  const current = $derived.by(() => {
    if (!tp || range.end <= range.start) return null;
    const s = range.end - 1;
    return {
      line: tp.line[s],
      kind: header!.kinds[tp.kind[s]],
      feature: header!.features?.[tp.feature[s]] ?? '',
      tool: header!.tools[tp.tool[s]]?.number ?? tp.tool[s],
      feed: tp.feed[s],
      width: tp.width[s] / 1000,
    };
  });

  const featureRows = $derived(
    (header?.features ?? [])
      .map((name, i) => ({ name, i, stat: header!.stats.features?.[name] }))
      .filter((r) => (r.stat?.segments ?? 0) > 0),
  );
  const totalExtruded = $derived(
    featureRows.reduce((sum, r) => sum + (r.stat?.length_mm ?? 0), 0) || 1,
  );
  const toolRows = $derived(
    (header?.tools ?? [])
      .map((t, i) => ({ t, i, stat: header!.stats.tools[i] }))
      .filter((r) => (r.stat?.segments ?? 0) > 0 || (r.t.filament_mm ?? 0) > 0),
  );

  // Keep the scene in sync with the sliders and toggles.
  $effect(() => {
    const { start, end } = range;
    scene?.setRange(start, end);
    const scrubbing = hi < layerMax || moves < layerLen(hi);
    const z = header?.layers?.[hi]?.z;
    scene?.setDimBelow(scrubbing && z != null ? z - 1e-3 : null);
  });
  // Read the state before touching the scene so each effect tracks it even while the scene
  // is null; `tp` re-applies the toggles to a freshly loaded toolpath.
  $effect(() => {
    const [mode, travel, pause, mask] = [colorMode, showTravel, showPause, [...hidden]];
    if (!scene || !tp) return;
    scene.setColorMode(mode);
    scene.setShowTravel(travel);
    scene.setPausePlanesVisible(pause);
    mask.forEach((h, i) => scene!.setFeatureVisible(i, !h));
  });
  $effect(() => {
    if (active) scene?.requestRender();
  });

  // Follow the shared current build (W9): pick its first print G-code and load it once the tab
  // is visible.
  let follow = $state('');
  $effect(() => {
    const cb = $currentBuild;
    if (!cb) return;
    const prefix = `/api/builds/${[cb.family, cb.variant, cb.stamp].map(encodeURIComponent).join('/')}/`;
    void listSources().then(() => {
      const source = sources.find((s) => s.url.startsWith(prefix));
      if (source && source.url !== loadedUrl) {
        selected = source.url;
        follow = source.url;
      }
    });
  });
  $effect(() => {
    if (!active || !scene || !follow) return;
    const url = follow;
    follow = '';
    void load(url);
  });

  onMount(() => {
    scene = new PrintScene(host!);
    statsOpen = host!.clientWidth > 700; // small screens: keep the canvas visible
    void listSources();
  });
  onDestroy(() => scene?.dispose());

  async function listSources() {
    const { data } = await api.GET('/api/builds');
    const found: Source[] = [];
    for (const b of data ?? []) {
      for (const f of b.files) {
        if (!/^fab\/print\/.+\.gcode$/i.test(f.name)) continue;
        const parts = [b.family, b.variant, b.stamp].map(encodeURIComponent).join('/');
        const name = f.name.split('/').map(encodeURIComponent).join('/');
        found.push({
          label: `${b.family}/${b.variant} ${b.stamp} · ${f.name.slice('fab/print/'.length)}`,
          url: `/api/builds/${parts}/toolpath/${name}?kind=print`,
        });
      }
    }
    sources = [...found, ...sources.filter((s) => s.url.startsWith('/api/uploads/'))];
  }

  async function load(url: string) {
    if (!url) return;
    error = '';
    status = 'Loading toolpath…';
    try {
      const t0 = performance.now();
      const next = await fetchToolpath(url);
      if (next.type !== 'print') throw new Error('not a print toolpath (kind=cnc)');
      const t1 = performance.now();
      status = 'Building geometry…';
      await new Promise((r) => setTimeout(r));
      scene!.load(next);
      timing = { fetch: t1 - t0, build: performance.now() - t1 };
      tp = next;
      loadedUrl = url;
      const layers = next.header.layer_offsets!;
      lo = 0;
      hi = layers.length - 2;
      moves = layerLen(hi);
      hidden = (next.header.features ?? []).map(() => false);
      status = '';
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      status = '';
    }
  }

  async function upload(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    error = '';
    status = `Uploading ${file.name}…`;
    try {
      const form = new FormData();
      form.append('file', file);
      const res = await fetch('/api/uploads?kind=gcode', { method: 'POST', body: form });
      const body = await res.json();
      if (!res.ok) throw new Error(String(body.detail ?? res.statusText));
      const url = `/api/uploads/${body.id}/toolpath?kind=print`;
      if (!sources.some((s) => s.url === url)) {
        sources = [...sources, { label: `upload · ${file.name}`, url }];
      }
      selected = url;
      await load(url);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      status = '';
    } finally {
      input.value = '';
    }
  }

  function setLayers([a, b]: number[]) {
    const topChanged = b !== hi;
    lo = a;
    hi = b;
    if (topChanged) moves = layerLen(b);
  }

  function step(n: number, bottom: boolean) {
    if (bottom) lo = Math.max(0, Math.min(hi, lo + n));
    else setLayers([lo, Math.max(lo, Math.min(layerMax, hi + n))]);
  }

  function onKey(e: KeyboardEvent) {
    if (!active || !tp || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    const t = e.target;
    if (t instanceof Element && t.closest('input, textarea, select, [contenteditable="true"]')) {
      return;
    }
    const big = e.shiftKey ? 10 : 1;
    switch (e.key) {
      case 'ArrowUp':
        step(1, false);
        break;
      case 'ArrowDown':
        step(-1, false);
        break;
      case 'ArrowRight':
        moves = Math.min(layerLen(hi), moves + big);
        break;
      case 'ArrowLeft':
        moves = Math.max(0, moves - big);
        break;
      default:
        return;
    }
    e.preventDefault();
  }

  function toggleFeature(i: number) {
    hidden[i] = !hidden[i];
  }

  function layerLabel(l: number): string {
    const z = header?.layers?.[l]?.z;
    return l === 0 ? 'start' : `${l} · ${z?.toFixed(2) ?? '?'}`;
  }
</script>

<svelte:window onkeydown={onKey} />

<div class="print">
  <div class="toolbar">
    <select
      bind:value={selected}
      onchange={() => load(selected)}
      onfocus={listSources}
      aria-label="G-code source"
    >
      <option value="" disabled>{sources.length ? 'Choose G-code…' : 'No build G-code yet'}</option>
      {#each sources as s (s.url)}
        <option value={s.url}>{s.label}</option>
      {/each}
    </select>
    <input
      class="upload"
      type="file"
      accept=".gcode,.gco,.g"
      aria-label="Upload G-code"
      title="Upload G-code"
      onchange={upload}
    />
    <select bind:value={colorMode} aria-label="Color by" disabled={!tp}>
      <option value="feature">By feature</option>
      <option value="tool">By filament</option>
    </select>
    <label><input type="checkbox" bind:checked={showTravel} disabled={!tp} /> Travel</label>
    {#if prime >= 0}
      <label>
        <input type="checkbox" checked={!hidden[prime]} onchange={() => toggleFeature(prime)} />
        Prime tower
      </label>
    {/if}
    {#if pauses.length}
      <label><input type="checkbox" bind:checked={showPause} /> Pause plane</label>
    {/if}
    <button class="outline secondary" onclick={() => scene?.fit()} disabled={!tp}>Fit</button>
  </div>

  <div class="stage" bind:this={stage}>
    <div class="canvas" bind:this={host}></div>
    <FullscreenButton target={stage} />

    {#if status || error || !tp}
      <div class="overlay center">
        {#if status}
          <p aria-busy="true">{status}</p>
        {:else if error}
          <p class="error">{error}</p>
        {:else}
          <p>Pick a build's print G-code or upload one.</p>
        {/if}
      </div>
    {/if}

    {#if tp && header}
      <div class="overlay side">
        <aside class="legend">
          {#if colorMode === 'feature'}
            {#each featureRows as r (r.i)}
              <button class="row" class:off={hidden[r.i]} onclick={() => toggleFeature(r.i)}>
                <span class="swatch" style={`background:${featureColor(r.name)}`}></span>
                <span class="name">{r.name}</span>
                <span class="num">
                  {((100 * (r.stat?.length_mm ?? 0)) / totalExtruded).toFixed(1)}%
                </span>
              </button>
            {/each}
          {:else}
            {#each toolRows as r (r.i)}
              <div class="row">
                <span class="swatch" style={`background:${cssColor(toolColor(r.t.color))}`}></span>
                <span class="name">T{r.t.number} {r.t.filament_type ?? ''}</span>
                <span class="num">{((r.t.filament_mm ?? 0) / 1000).toFixed(2)} m</span>
              </div>
            {/each}
          {/if}
          <button class="row" class:off={!showTravel} onclick={() => (showTravel = !showTravel)}>
            <span class="swatch" style={`background:${TRAVEL_COLOR}`}></span>
            <span class="name">Travel</span>
            <span class="num">
              {((header.stats.kinds.travel?.length_mm ?? 0) / 1000).toFixed(1)} m
            </span>
          </button>
        </aside>

        <details class="stats" bind:open={statsOpen}>
          <summary>
            <span class="title">{sourceLabel || header.source.name || 'Toolpath'}</span>
          </summary>
          <dl>
            <dt>Layers</dt>
            <dd>{header.layer_count}</dd>
            <dt>Print time (slicer)</dt>
            <dd>{formatDuration(header.source.estimated_time_s)}</dd>
            <dt>Filament</dt>
            <dd>
              {#each toolRows as r (r.i)}
                <div>
                  T{r.t.number}
                  {r.t.filament_type ?? ''}: {((r.t.filament_mm ?? 0) / 1000).toFixed(2)} m
                </div>
              {/each}
            </dd>
            <dt>Tool changes / pauses</dt>
            <dd>{toolChanges.length} / {pauses.length}</dd>
            <dt>Segments</dt>
            <dd>{header.segments.toLocaleString()}</dd>
            <dt>Load</dt>
            <dd>{(timing.fetch / 1000).toFixed(2)} s fetch · {timing.build.toFixed(0)} ms build</dd>
            {#if current}
              <dt>Move</dt>
              <dd>
                line {current.line} · {current.kind}{current.feature ? ` · ${current.feature}` : ''} ·
                T{current.tool}
                {#if current.feed}· F{current.feed.toFixed(0)}{/if}
                {#if current.width}· w {current.width.toFixed(2)}{/if}
              </dd>
            {/if}
          </dl>
        </details>
      </div>

      <div class="overlay layers">
        <RangeSlider
          vertical
          min={0}
          max={layerMax}
          values={[lo, hi]}
          ticks={layerTickMarks}
          label="Layers"
          format={layerLabel}
          onchange={setLayers}
        />
      </div>

      <div class="overlay moves">
        <RangeSlider
          min={0}
          max={layerLen(hi)}
          values={[moves]}
          ticks={moveTickMarks}
          label="Moves in the top layer"
          format={(v) => `${v} / ${layerLen(hi)}`}
          onchange={([v]) => (moves = v)}
        />
      </div>
    {/if}
  </div>
</div>

<style>
  .print {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
    min-height: 0;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    align-items: center;
    padding: 0.4rem 0.5rem;
    font-size: 0.85rem;
    --pico-form-element-spacing-vertical: 0.2rem;
    --pico-form-element-spacing-horizontal: 0.5rem;
  }
  .toolbar input[type='file']::file-selector-button {
    font-size: 0.85rem;
    padding: 0.2rem 0.6rem;
  }
  .toolbar > * {
    margin: 0;
    width: auto;
  }
  .toolbar select,
  .toolbar input[type='file'] {
    padding-top: 0.25rem;
    padding-bottom: 0.25rem;
    font-size: 0.85rem;
    height: auto;
  }
  .toolbar select {
    max-width: min(100%, 28rem);
  }
  .toolbar button {
    padding: 0.3rem 0.8rem;
  }
  .upload {
    max-width: 16rem;
    font-size: 0.85rem;
  }
  .stage {
    position: relative;
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }
  .canvas {
    position: absolute;
    inset: 0;
  }
  .overlay {
    position: absolute;
    color: #e8eaee;
    font-size: 0.8rem;
  }
  .center {
    inset: 0;
    display: grid;
    place-items: center;
    pointer-events: none;
  }
  .error {
    color: #ff8a80;
  }
  .side {
    top: 0.5rem;
    left: 0.5rem;
    max-height: calc(100% - 3.5rem);
    overflow: auto;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.5rem;
  }
  .legend {
    display: flex;
    flex-direction: column;
    gap: 2px;
    background: rgb(0 0 0 / 0.45);
    border-radius: 0.4rem;
    padding: 0.4rem;
  }
  .row {
    all: unset;
    display: grid;
    grid-template-columns: 0.9rem 1fr auto;
    gap: 0.5rem;
    align-items: center;
    padding: 1px 0.3rem;
    border-radius: 0.25rem;
    cursor: pointer;
  }
  div.row {
    cursor: default;
  }
  .row:hover {
    background: rgb(255 255 255 / 0.08);
  }
  .row.off {
    opacity: 0.4;
    text-decoration: line-through;
  }
  .swatch {
    width: 0.9rem;
    height: 0.9rem;
    border-radius: 0.2rem;
    border: 1px solid rgb(255 255 255 / 0.35);
  }
  .num {
    font-variant-numeric: tabular-nums;
    opacity: 0.8;
  }
  .stats {
    max-width: 20rem;
    background: rgb(0 0 0 / 0.45);
    border-radius: 0.4rem;
    padding: 0.3rem 0.6rem;
    margin: 0;
    border: 0;
  }
  .stats summary {
    color: inherit;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  details.stats,
  .title,
  details.stats dt,
  details.stats dd {
    color: #e8eaee;
  }
  .stats dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0 0.6rem;
    margin: 0.3rem 0 0;
  }
  .stats dt {
    opacity: 0.7;
    font-weight: normal;
  }
  .stats dd {
    margin: 0;
  }
  .layers {
    top: 2.75rem;
    bottom: 3.5rem;
    right: 0.5rem;
  }
  .moves {
    left: 1rem;
    right: 5rem;
    bottom: 0.5rem;
  }
</style>
