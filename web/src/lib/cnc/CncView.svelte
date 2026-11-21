<script lang="ts">
  /**
   * CNC toolpath preview (W7, #68): a build's `fab/cnc/*.nc` (one file, or all of them as
   * one job) or uploaded files, colored by tool, with a job-time scrubber that recolors
   * done vs remaining, a tool marker, stock and bounding box, view presets and tool stats.
   * Follows the shared `currentBuild` (#97): picking a build in the drawer loads all of its
   * NC files as one job; the source picker and uploads still choose anything else.
   */
  import { onDestroy, onMount } from 'svelte';
  import { api } from '../client';
  import { currentBuild } from '../stores/build';
  import RangeSlider, { type Tick } from '../print/RangeSlider.svelte';
  import FullscreenButton from '../ui/FullscreenButton.svelte';
  import { fetchToolpath, type CncToolpath } from '../toolpath/reader';
  import {
    KIND_PLUNGE,
    buildJob,
    compareNames,
    formatClock,
    formatLength,
    playheadAt,
    stepTime,
    toolLabel,
    type CncJob,
  } from './model';
  import { BBOX_COLOR, MARKER_COLOR, STOCK_COLOR, cncToolColor } from './palette';
  import { CncScene, type ViewPreset } from './scene';
  import {
    cncSources,
    sourceForBuild,
    type BuildListing,
    type FileRef,
    type Source,
  } from './sources';

  let { active = true }: { active?: boolean } = $props();

  const STEP = 10; // slider units per second
  const SPEEDS = [1, 10, 60, 300];
  const VIEWS: ViewPreset[] = ['top', 'front', 'side', 'iso'];

  let host: HTMLDivElement | undefined = $state();
  let stage: HTMLDivElement | undefined = $state();
  let scene = $state.raw<CncScene | null>(null);
  let builds: BuildListing[] = $state.raw([]);
  let buildSources: Source[] = $state([]);
  let uploadSources: Source[] = $state([]);
  let selected = $state('');
  let loadedId = $state('');
  let status = $state('');
  let error = $state('');
  let notice = $state('');
  let job = $state.raw<CncJob | null>(null);
  let timing = $state({ fetch: 0, build: 0 });

  let time = $state(0);
  let playing = $state(false);
  let speed = $state(10);
  let view: ViewPreset = $state('iso');
  let showRapids = $state(true);
  let remaining: 'dim' | 'hide' = $state('dim');
  let showStock = $state(true);
  let showBox = $state(true);
  let showZero = $state(true);
  let hidden: boolean[] = $state([]);
  let statsOpen = $state(true);

  const sources = $derived([...buildSources, ...uploadSources]);
  const sourceLabel = $derived(sources.find((s) => s.id === loadedId)?.label ?? '');
  const duration = $derived(job?.duration ?? 0);
  const head = $derived(job ? playheadAt(job, time) : null);

  const current = $derived.by(() => {
    if (!job || !head || head.segment < 0) return null;
    const part = job.parts[head.part];
    const tp = part.tp;
    const s = head.segment;
    const kind = tp.header.kinds[tp.kind[s]] ?? '?';
    return {
      file: part.label,
      line: tp.line[s],
      kind: head.waiting ? 'waiting (tool change / dwell)' : kind,
      plunge: tp.kind[s] === KIND_PLUNGE,
      tool: job.tools[head.tool],
      feed: tp.feed[s],
      pos: head.pos,
    };
  });

  const ticks: Tick[] = $derived(
    (job?.markers ?? []).map((m) => ({
      value: Math.round(m.time * STEP),
      title: `${formatClock(m.time)} · ${m.title}`,
      color: m.type === 'pause' ? MARKER_COLOR : cncToolColor(m.tool ?? 0),
      wide: m.type !== 'tool_change',
    })),
  );

  $effect(() => {
    const [t, h] = [time, head];
    if (!scene || !job) return;
    scene.setTime(t);
    const tool = h ? job.tools[h.tool] : undefined;
    scene.setToolMarker(h?.pos ?? null, cncToolColor(h?.tool ?? 0), tool?.entry.diameter);
  });
  $effect(() => {
    const state = [showRapids, remaining, showStock, showBox, showZero, [...hidden]] as const;
    if (!scene || !job) return;
    scene.setShowRapids(state[0]);
    scene.setRemaining(state[1]);
    scene.setDecorVisible('stock', state[2]);
    scene.setDecorVisible('bbox', state[3]);
    scene.setDecorVisible('zero', state[4]);
    state[5].forEach((h, i) => scene!.setToolVisible(i, !h));
  });
  $effect(() => {
    if (active) scene?.requestRender();
    else playing = false;
  });

  // Playback: advance job time at `speed`× real time.
  $effect(() => {
    if (!playing) return;
    let last = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const next = time + ((now - last) / 1000) * speed;
      last = now;
      if (next >= duration) {
        time = duration;
        playing = false;
        return;
      }
      time = next;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  });

  // Follow the shared current build (W9): list its NC files and, once the tab is visible,
  // load them all as one job. `listSources` resolves after this effect has run, so the state
  // it reads and writes there is not tracked (no reload loop).
  let follow = $state('');
  $effect(() => {
    const cb = $currentBuild;
    if (!cb) return;
    void listSources().then(() => {
      const source = sourceForBuild(buildSources, builds, cb);
      notice = source
        ? ''
        : `${cb.family}/${cb.variant} ${cb.stamp} has no CNC G-code (lure fab --format cnc)`;
      if (source && source.id !== loadedId) {
        selected = source.id;
        follow = source.id;
      }
    });
  });
  $effect(() => {
    if (!active || !scene || !follow) return;
    const id = follow;
    follow = '';
    void load(id);
  });

  onMount(() => {
    scene = new CncScene(host!);
    statsOpen = host!.clientWidth > 1000;
    void listSources();
  });
  onDestroy(() => scene?.dispose());

  async function listSources() {
    const { data } = await api.GET('/api/builds');
    builds = data ?? [];
    buildSources = cncSources(builds);
  }

  async function load(id: string) {
    const source = sources.find((s) => s.id === id);
    if (!source) return;
    error = '';
    notice = '';
    playing = false;
    status = `Loading ${source.files.length > 1 ? `${source.files.length} toolpaths` : 'toolpath'}…`;
    try {
      const t0 = performance.now();
      const tps = await Promise.all(source.files.map((f) => fetchToolpath(f.url)));
      const files = tps.map((tp, i) => {
        if (tp.type !== 'cnc') throw new Error(`${source.files[i].label} is not CNC G-code`);
        return { label: source.files[i].label, tp: tp as CncToolpath };
      });
      const t1 = performance.now();
      status = 'Building geometry…';
      await new Promise((r) => setTimeout(r));
      const next = buildJob(files);
      scene!.load(next);
      scene!.setView(view);
      timing = { fetch: t1 - t0, build: performance.now() - t1 };
      hidden = next.tools.map(() => false);
      job = next;
      loadedId = id;
      time = next.duration;
      status = '';
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      status = '';
    }
  }

  async function upload(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const picked = [...(input.files ?? [])].sort((a, z) => compareNames(a.name, z.name));
    if (!picked.length) return;
    error = '';
    try {
      const files: FileRef[] = [];
      for (const file of picked) {
        status = `Uploading ${file.name}…`;
        const form = new FormData();
        form.append('file', file);
        const res = await fetch('/api/uploads?kind=gcode', { method: 'POST', body: form });
        const body = await res.json();
        if (!res.ok) throw new Error(`${file.name}: ${String(body.detail ?? res.statusText)}`);
        files.push({ label: file.name, url: `/api/uploads/${body.id}/toolpath?kind=cnc` });
      }
      const id = `upload:${files.map((f) => f.url).join('|')}`;
      const label =
        files.length > 1 ? `upload · ${files.length} files` : `upload · ${files[0].label}`;
      if (!uploadSources.some((s) => s.id === id)) {
        uploadSources = [...uploadSources, { id, label, files }];
      }
      selected = id;
      await load(id);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      status = '';
    } finally {
      input.value = '';
    }
  }

  function setView(v: ViewPreset) {
    view = v;
    scene?.setView(v);
  }

  function togglePlay() {
    if (!job) return;
    if (!playing && time >= duration) time = 0;
    playing = !playing;
  }

  function onKey(e: KeyboardEvent) {
    if (!active || !job || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    const t = e.target;
    if (t instanceof Element && t.closest('input, textarea, select, [contenteditable="true"]')) {
      return;
    }
    const n = e.shiftKey ? 10 : 1;
    switch (e.key) {
      case 'ArrowRight':
        playing = false;
        time = stepTime(job, time, n);
        break;
      case 'ArrowLeft':
        playing = false;
        time = stepTime(job, time, -n);
        break;
      case 'Home':
        time = 0;
        break;
      case 'End':
        time = duration;
        break;
      case ' ':
        togglePlay();
        break;
      default:
        return;
    }
    e.preventDefault();
  }

  function fmtPos(p: readonly number[]): string {
    return p.map((v, k) => `${'XYZ'[k]}${v.toFixed(2)}`).join(' ');
  }
</script>

<svelte:window onkeydown={onKey} />

<div class="cnc">
  <div class="toolbar">
    <select
      bind:value={selected}
      onchange={() => load(selected)}
      onfocus={listSources}
      aria-label="CNC G-code source"
    >
      <option value="" disabled>{sources.length ? 'Choose toolpath…' : 'No build NC yet'}</option>
      {#each sources as s (s.id)}
        <option value={s.id}>{s.label}</option>
      {/each}
    </select>
    <input
      class="upload"
      type="file"
      multiple
      accept=".nc,.ngc,.tap,.cnc,.gcode"
      aria-label="Upload NC files"
      title="Upload one or more NC files (several play as one job)"
      onchange={upload}
    />
    {#if notice && job}<small class="notice">{notice}</small>{/if}
    <label><input type="checkbox" bind:checked={showRapids} disabled={!job} /> Rapids</label>
    <select bind:value={remaining} aria-label="Remaining moves" disabled={!job}>
      <option value="dim">Remaining: dim</option>
      <option value="hide">Remaining: hide</option>
    </select>
    {#if job?.stock}
      <label><input type="checkbox" bind:checked={showStock} /> Stock</label>
    {/if}
    <label><input type="checkbox" bind:checked={showBox} disabled={!job} /> Bounds</label>
    <label><input type="checkbox" bind:checked={showZero} disabled={!job} /> Zero</label>
    <div class="views" role="group" aria-label="View">
      {#each VIEWS as v (v)}
        <button class:outline={view !== v} class="secondary" onclick={() => setView(v)}>
          {v[0].toUpperCase() + v.slice(1)}
        </button>
      {/each}
    </div>
  </div>

  <div class="stage" bind:this={stage}>
    <div class="canvas" bind:this={host}></div>
    <FullscreenButton target={stage} />

    {#if status || error || !job}
      <div class="overlay center">
        {#if status}
          <p aria-busy="true">{status}</p>
        {:else if error}
          <p class="error">{error}</p>
        {:else if notice}
          <p>{notice}</p>
        {:else}
          <p>Pick a build's NC files or upload some.</p>
        {/if}
      </div>
    {/if}

    {#if job}
      <div class="overlay side">
        <aside class="legend">
          <table>
            <thead>
              <tr>
                <th>Tool</th>
                <th title="Feed and plunge path length">Cut</th>
                <th>Rapid</th>
                <th title="Motion time (feeds at F, rapids at the machine's rapid rates)">
                  Time
                </th>
              </tr>
            </thead>
            <tbody>
              {#each job.tools as t, i (t.key)}
                <tr
                  class="row"
                  class:off={hidden[i]}
                  onclick={() => (hidden[i] = !hidden[i])}
                  title={[t.entry.description, t.files.join(', ')].filter(Boolean).join(' · ')}
                >
                  <td>
                    <span class="swatch" style={`background:${cncToolColor(i)}`}></span>
                    {toolLabel(t)}
                    {#if t.entry.spindle_rpm}<small>{t.entry.spindle_rpm} rpm</small>{/if}
                  </td>
                  <td class="num">{formatLength(t.cutLength)}</td>
                  <td class="num">{formatLength(t.rapidLength)}</td>
                  <td class="num">{formatClock(t.time)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </aside>

        <details class="stats" bind:open={statsOpen}>
          <summary>
            <span class="title">{sourceLabel || 'Toolpath'} · {formatClock(job.duration)}</span>
          </summary>
          <dl>
            <dt>Est. time</dt>
            <dd>
              {formatClock(job.duration)}
              <small>
                ({formatClock(job.motionTime)} motion + {formatClock(job.waitTime)} tool changes and dwells)
              </small>
            </dd>
            <dt>Tool changes</dt>
            <dd>{job.toolChanges}</dd>
            {#if job.parts.length > 1}
              <dt>Files</dt>
              <dd>{job.parts.length}</dd>
            {/if}
            {#if job.stock}
              <dt><span class="key" style={`color:${STOCK_COLOR}`}>■</span> Stock</dt>
              <dd>
                {(job.stock.max[0] - job.stock.min[0]).toFixed(1)} ×
                {(job.stock.max[1] - job.stock.min[1]).toFixed(1)} ×
                {(job.stock.max[2] - job.stock.min[2]).toFixed(1)} mm
              </dd>
            {/if}
            {#if job.active}
              <dt><span class="key" style={`color:${BBOX_COLOR}`}>■</span> Cuts</dt>
              <dd>
                X {job.active.min[0].toFixed(1)}…{job.active.max[0].toFixed(1)}, Y
                {job.active.min[1].toFixed(1)}…{job.active.max[1].toFixed(1)}, Z
                {job.active.min[2].toFixed(2)}…{job.active.max[2].toFixed(2)}
              </dd>
            {/if}
            <dt>Segments</dt>
            <dd>{job.segments.toLocaleString()}</dd>
            <dt>Load</dt>
            <dd>{(timing.fetch / 1000).toFixed(2)} s fetch · {timing.build.toFixed(0)} ms build</dd>
            {#if current}
              <dt>Move</dt>
              <dd>
                {#if job.parts.length > 1}{current.file} ·{/if}
                line {current.line} · {current.plunge ? 'plunge' : current.kind} ·
                {toolLabel(current.tool)}
                {#if current.feed}· F{current.feed.toFixed(0)}{/if}
              </dd>
              <dt>Tool tip</dt>
              <dd class="num">{fmtPos(current.pos)}</dd>
            {/if}
          </dl>
        </details>
      </div>

      <div class="overlay scrub">
        <button
          class="play secondary"
          onclick={togglePlay}
          aria-label={playing ? 'Pause' : 'Play'}
          title="Play/pause (Space)"
        >
          {playing ? '❚❚' : '▶'}
        </button>
        <select bind:value={speed} aria-label="Playback speed">
          {#each SPEEDS as s (s)}
            <option value={s}>{s}×</option>
          {/each}
        </select>
        <div class="slider">
          <RangeSlider
            min={0}
            max={Math.round(duration * STEP)}
            values={[Math.round(time * STEP)]}
            {ticks}
            label="Job progress"
            format={(v) => `${formatClock(v / STEP)} / ${formatClock(duration)}`}
            onchange={([v]) => {
              playing = false;
              time = v / STEP;
            }}
          />
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .cnc {
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
  .views {
    display: flex;
    gap: 0.25rem;
  }
  .views button,
  .play {
    margin: 0;
    width: auto;
    padding: 0.25rem 0.6rem;
    font-size: 0.85rem;
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
  .canvas :global(.cnc-label) {
    font-size: 0.75rem;
    padding: 0 0.3rem;
    border-radius: 0.2rem;
    background: rgb(0 0 0 / 0.55);
    white-space: nowrap;
  }
  .canvas :global(.cnc-label-dim) {
    color: #7fb0ff;
  }
  .canvas :global(.cnc-label-zero) {
    color: #e8eaee;
    transform: translate(0.6rem, 0.6rem);
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
  .notice {
    color: var(--pico-muted-color);
  }
  .side {
    top: 0.5rem;
    left: 0.5rem;
    max-height: calc(100% - 4rem);
    max-width: min(26rem, calc(100% - 1rem));
    overflow: auto;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.5rem;
  }
  .legend,
  .stats {
    background: rgb(0 0 0 / 0.45);
    border-radius: 0.4rem;
    padding: 0.3rem 0.6rem;
    margin: 0;
    border: 0;
  }
  .legend table {
    margin: 0;
    font-size: 0.8rem;
    --pico-background-color: transparent;
  }
  .legend th,
  .legend td {
    padding: 0.1rem 0.4rem;
    color: #e8eaee;
    background: transparent;
    border-color: rgb(255 255 255 / 0.12);
    white-space: nowrap;
  }
  .legend th {
    opacity: 0.7;
    font-weight: normal;
  }
  .legend small {
    opacity: 0.7;
    margin-left: 0.3rem;
  }
  .row {
    cursor: pointer;
  }
  .row:hover td {
    background: rgb(255 255 255 / 0.08);
  }
  .row.off {
    opacity: 0.4;
    text-decoration: line-through;
  }
  .swatch {
    display: inline-block;
    vertical-align: -0.1rem;
    width: 0.8rem;
    height: 0.8rem;
    margin-right: 0.3rem;
    border-radius: 0.2rem;
    border: 1px solid rgb(255 255 255 / 0.35);
  }
  .num {
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
  .stats {
    max-width: 24rem;
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
  .stats small {
    opacity: 0.75;
  }
  .scrub {
    left: 0.5rem;
    right: 0.5rem;
    bottom: 0.5rem;
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  .scrub select {
    margin: 0;
    width: auto;
    padding: 0.2rem 1.8rem 0.2rem 0.5rem;
    font-size: 0.8rem;
  }
  .slider {
    flex: 1;
    min-width: 0;
    padding-right: 2.5rem;
  }
</style>
