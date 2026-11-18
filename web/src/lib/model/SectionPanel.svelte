<script lang="ts">
  // Section panel: plane (axis + world offset) → GET …/section → outline, areas, perimeter.
  import SectionSvg from './SectionSvg.svelte';
  import { AXES, type Axis, type ModelInfo, type Section } from './types';

  interface Props {
    base: string;
    info: ModelInfo;
    axis: Axis;
    offset: number;
    /** Drive the viewer's clip plane from this panel (and follow its slider). */
    sync: boolean;
  }

  let {
    base,
    info,
    axis = $bindable(),
    offset = $bindable(),
    sync = $bindable(),
  }: Props = $props();

  let part = $state('all');
  let exact = $state(false);
  let section: Section | null = $state(null);
  let error = $state('');
  let busy = $state(false);

  const range = $derived.by(() => {
    const i = AXES.indexOf(axis);
    const lo = info.bbox_min?.[i] ?? -100;
    const hi = info.bbox_max?.[i] ?? 100;
    return { lo: Math.floor(lo * 10) / 10, hi: Math.ceil(hi * 10) / 10 };
  });

  function setAxis(a: Axis) {
    axis = a;
    offset = Math.round(((range.lo + range.hi) / 2) * 10) / 10;
  }

  $effect(() => {
    if (!info.parts.some((p) => p.name === part)) part = 'all';
  });

  $effect(() => {
    const url =
      `${base}/section?axis=${axis}&offset=${offset}` +
      `&part=${encodeURIComponent(part)}&exact=${exact}`;
    const ctrl = new AbortController();
    const timer = setTimeout(
      async () => {
        busy = true;
        try {
          const r = await fetch(url, { signal: ctrl.signal });
          const body = await r.json();
          if (!r.ok) throw new Error(body?.detail ?? r.statusText);
          section = body as Section;
          error = '';
        } catch (e) {
          if (!ctrl.signal.aborted) error = e instanceof Error ? e.message : String(e);
        } finally {
          if (!ctrl.signal.aborted) busy = false;
        }
      },
      exact ? 400 : 80,
    );
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  });

  const fmt = (x: number, d = 1) => x.toLocaleString(undefined, { maximumFractionDigits: d });
</script>

<article class="section-panel">
  <header><strong>Section</strong></header>
  <div role="group">
    {#each AXES as a (a)}
      <button class:outline={axis !== a} onclick={() => setAxis(a)}>{a.toUpperCase()}</button>
    {/each}
  </div>
  <label>
    {axis} =
    <input class="num" type="number" step="0.1" min={range.lo} max={range.hi} bind:value={offset} />
    mm
    <input type="range" step="0.1" min={range.lo} max={range.hi} bind:value={offset} />
  </label>
  <div class="row">
    <label>
      Part
      <select bind:value={part}>
        <option value="all">all</option>
        {#each info.parts as p (p.name)}<option value={p.name}>{p.name}</option>{/each}
      </select>
    </label>
    <label><input type="checkbox" role="switch" bind:checked={exact} /> exact</label>
    <label><input type="checkbox" role="switch" bind:checked={sync} /> clip viewer</label>
  </div>

  {#if error}
    <p><small class="err">{error}</small></p>
  {/if}
  {#if section}
    <div aria-busy={busy}>
      <table>
        <tbody>
          <tr><th>Area</th><td>{fmt(section.area_mm2)} mm²</td></tr>
          <tr><th>Void area</th><td>{fmt(section.void_area_mm2)} mm²</td></tr>
          <tr><th>Perimeter</th><td>{fmt(section.perimeter_mm)} mm</td></tr>
          <tr>
            <th>Method</th>
            <td>
              {section.method} · {fmt(section.elapsed_ms, 0)} ms
              {#if section.method === 'exact' && section.delta_pct != null}
                <br /><small class:err={section.differs}>
                  {section.delta_pct >= 0 ? '+' : ''}{fmt(section.delta_pct, 2)} % vs mesh
                  {#if section.differs}(differs by more than 1 %; exact is unreliable near loft
                    tips){/if}
                </small>
              {/if}
            </td>
          </tr>
        </tbody>
      </table>
      <SectionSvg {section} />
    </div>
  {:else if busy}
    <p aria-busy="true">Cutting…</p>
  {/if}
</article>

<style>
  .section-panel {
    margin: 0;
    padding: 0.75rem;
    font-size: 0.875rem;
  }
  .section-panel header {
    margin: -0.75rem -0.75rem 0.5rem;
    padding: 0.5rem 0.75rem;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 0 1rem;
    align-items: end;
  }
  .row select,
  [role='group'] button {
    margin-bottom: 0.5rem;
    padding: 0.25rem 0.6rem;
    font-size: 0.85rem;
    height: auto;
  }
  [role='group'] {
    margin-bottom: 0.25rem;
  }
  .num {
    width: 6rem;
    display: inline-block;
    padding: 0.2rem 0.4rem;
    height: auto;
    margin: 0;
  }
  table {
    margin: 0 0 0.5rem;
  }
  th,
  td {
    padding: 0.2rem 0.4rem;
  }
  .err {
    color: #d9534f;
  }
</style>
