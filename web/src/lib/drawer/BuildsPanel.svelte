<!-- Build history per variant (GET /api/builds?family=&variant=) with report summaries; picking a
     build makes it `currentBuild` (Model/Print tabs follow). Shows its downloads, runs
     `lure fab <build> --format print|cnc` as jobs, and compares two builds. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../client';
  import type { components } from '../api';
  import { errorText, submitJob } from '../jobs';
  import { currentBuild, setCurrentBuild } from '../stores/build';
  import CompareView from './CompareView.svelte';
  import { bytes, num, stampSha, stampTime, togglePick } from './format';
  import { buildsVersion, bump, jobsVersion, selectedJob } from './state';

  type BuildSummary = components['schemas']['BuildSummary'];
  type Download = components['schemas']['Download'];

  let builds: BuildSummary[] = $state([]);
  let variantKey = $state('');
  let picked: string[] = $state([]);
  let error = $state('');
  let message = $state('');

  const key = (b: Pick<BuildSummary, 'family' | 'variant' | 'stamp'>) =>
    `${b.family}/${b.variant}/${b.stamp}`;
  const vkey = (b: Pick<BuildSummary, 'family' | 'variant'>) => `${b.family}/${b.variant}`;

  const variants = $derived([...new Set(builds.map(vkey))].sort());
  const history = $derived(builds.filter((b) => vkey(b) === variantKey));
  const currentKey = $derived($currentBuild ? key($currentBuild) : '');
  const current = $derived(builds.find((b) => key(b) === currentKey) ?? null);

  async function refresh() {
    const { data, error: e, response } = await api.GET('/api/builds');
    if (!data) {
      error = errorText(e, response);
      return;
    }
    error = '';
    builds = data;
    picked = picked.filter((k) => data.some((b) => key(b) === k));
    if (!variants.includes(variantKey)) variantKey = data.length ? vkey(data[0]) : '';
  }

  $effect(() => {
    void $buildsVersion;
    void refresh();
  });

  // Follow the current build (set here, by the editor's Build action or by the Model tab).
  $effect(() => {
    const cb = $currentBuild;
    if (!cb) return;
    untrack(() => {
      if (!builds.some((b) => key(b) === key(cb))) void refresh();
      variantKey = vkey(cb);
    });
  });

  function select(b: BuildSummary) {
    setCurrentBuild(b.path);
  }

  async function fab(b: BuildSummary, format: 'print' | 'cnc') {
    message = '';
    try {
      const op = format === 'print' ? 'fab_print' : 'fab_cnc';
      const id = await submitJob({ op, args: { build: b.path } });
      selectedJob.set(id);
      bump(jobsVersion);
    } catch (err) {
      message = (err as Error).message;
    }
  }

  const ORDER = ['STEP', 'STL', '3MF', 'GLB', 'G-code', 'NC', 'ZIP'];
  function grouped(downloads: Download[]): [string, Download[]][] {
    const groups: Record<string, Download[]> = {};
    for (const d of downloads) (groups[d.format] ??= []).push(d);
    const rank = (f: string) => (ORDER.includes(f) ? ORDER.indexOf(f) : ORDER.length);
    return Object.entries(groups).sort(([a], [b]) => rank(a) - rank(b));
  }

  const short = (label: string) => label.replace(/^(model|fab\/print|fab\/cnc)\//, '');
</script>

<div class="builds">
  <div class="head">
    <strong>Builds</strong>
    <select bind:value={variantKey} aria-label="Variant">
      {#each variants as v (v)}<option value={v}>{v}</option>{/each}
    </select>
    <small>{history.length} {history.length === 1 ? 'build' : 'builds'}</small>
    <button type="button" class="mini" onclick={refresh}>refresh</button>
  </div>
  {#if error}<small class="error-text">{error}</small>{/if}
  {#if !builds.length && !error}
    <p><small>No builds yet (<code>lure build &lt;variant&gt;</code>).</small></p>
  {:else}
    <div class="scroll">
      <table class="compact" data-testid="build-history">
        <thead>
          <tr>
            <th title="Pick two builds to compare">A/B</th>
            <th>Built</th>
            <th>Status</th>
            <th class="num">Mass g (oz)</th>
            <th class="num">g/cm³</th>
            <th class="num" title="CB above CG, z (mm); self-righting when > 0">CB−CG z</th>
            <th class="num" title="CG − CB along x (mm)">CG−CB x</th>
            <th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {#each history as b (key(b))}
            {@const r = b.report}
            <tr class:selected={key(b) === currentKey} onclick={() => select(b)}>
              <td>
                <input
                  type="checkbox"
                  aria-label="compare"
                  checked={picked.includes(key(b))}
                  onclick={(e) => e.stopPropagation()}
                  onchange={() => (picked = togglePick(picked, key(b)))}
                />
              </td>
              <td title={b.stamp}>
                {stampTime(b.stamp)} <small><code>{stampSha(b.stamp)}</code></small>
                {#if b.latest}<span class="badge">latest</span>{/if}
              </td>
              <td><span class="badge" data-status={b.status}>{b.status ?? '?'}</span></td>
              <td class="num">
                {num(r?.mass_g, 1)}
                {#if r?.mass_oz != null}<small>({num(r.mass_oz, 2)})</small>{/if}
              </td>
              <td class="num">{num(r?.density_g_cm3, 3)}</td>
              <td class="num">{num(r?.cb_above_cg_mm, 2)}</td>
              <td class="num">{num(r?.cg_minus_cb_mm?.[0], 2)}</td>
              <td>
                {#if r?.verdict}
                  <span class="badge" data-verdict={r.verdict.status} title={r.verdict.label}>
                    {r.verdict.status}
                  </span>
                {:else if r?.error}
                  <small class="error-text" title={r.error}>error</small>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if picked.length === 2}
      <CompareView a={picked[0]} b={picked[1]} onclose={() => (picked = [])} />
    {:else if picked.length === 1}
      <small>Pick one more build to compare.</small>
    {/if}
  {/if}

  {#if current}
    <div class="detail" data-testid="build-detail">
      <div class="head">
        <strong>{current.variant}</strong>
        <small><code>{current.path}</code></small>
      </div>
      {#if current.report?.error}<small class="error-text">{current.report.error}</small>{/if}
      {#if current.warnings.length}
        {@const unique = [...new Set(current.warnings)]}
        <details>
          <summary>
            <small>⚠ {unique.length} {unique.length === 1 ? 'warning' : 'warnings'}</small>
          </summary>
          {#each unique as w (w)}<small class="warn-text">{w}</small>{/each}
        </details>
      {/if}
      <div class="downloads">
        {#each grouped(current.downloads) as [format, items] (format)}
          <div>
            <small><strong>{format}</strong></small>
            {#each items as d (d.href)}
              <a href={d.href} download title={d.label}>
                {short(d.label)}{#if d.size != null}&nbsp;<small>{bytes(d.size)}</small>{/if}
              </a>
            {/each}
          </div>
        {:else}
          <small>No exportable files.</small>
        {/each}
      </div>
      <div class="actions">
        <button
          type="button"
          class="mini"
          title={`lure fab ${current.path} --format print`}
          onclick={() => fab(current!, 'print')}
        >
          fab print
        </button>
        <button
          type="button"
          class="mini"
          title={`lure fab ${current.path} --format cnc`}
          onclick={() => fab(current!, 'cnc')}
        >
          fab cnc
        </button>
        <a href={`/api/builds/${key(current)}/files/report.md`} target="_blank" rel="noreferrer">
          <small>report.md</small>
        </a>
      </div>
      {#if message}<small class="error-text">{message}</small>{/if}
    </div>
  {/if}
</div>
