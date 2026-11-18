<script lang="ts">
  // 2D outline of a section: loops in the plane's (u, v) world coordinates, v up.
  import type { Section } from './types';

  let { section }: { section: Section } = $props();

  const view = $derived.by(() => {
    let umin = Infinity,
      umax = -Infinity,
      vmin = Infinity,
      vmax = -Infinity;
    for (const lp of section.loops)
      for (const [u, v] of lp.points) {
        umin = Math.min(umin, u);
        umax = Math.max(umax, u);
        vmin = Math.min(vmin, v);
        vmax = Math.max(vmax, v);
      }
    if (!Number.isFinite(umin)) return null;
    const w = umax - umin,
      h = vmax - vmin;
    const pad = Math.max(w, h) * 0.04 + 0.5;
    return {
      umin,
      umax,
      vmin,
      vmax,
      w,
      h,
      box: `${umin - pad} ${-vmax - pad} ${w + 2 * pad} ${h + 2 * pad}`,
      stroke: Math.max(w, h) / 300,
    };
  });

  const ring = (pts: [number, number][]) =>
    pts.length ? `M${pts.map(([u, v]) => `${u},${-v}`).join('L')}Z` : '';

  // One even-odd path per part fills material and leaves voids open.
  const fills = $derived.by(() => {
    const byPart: Record<string, string> = {};
    for (const lp of section.loops) byPart[lp.part] = (byPart[lp.part] ?? '') + ring(lp.points);
    return Object.entries(byPart).map(([part, d]) => ({ part, d }));
  });
</script>

{#if view}
  <figure>
    <svg viewBox={view.box} preserveAspectRatio="xMidYMid meet" role="img">
      <title>Section {section.axis} = {section.offset.toFixed(1)} mm</title>
      {#each fills as f (f.part)}
        <path class="fill" d={f.d} fill-rule="evenodd" />
      {/each}
      {#each section.loops as lp, i (i)}
        <path
          class={lp.kind}
          d={ring(lp.points)}
          stroke-width={view.stroke * (lp.kind === 'void' ? 1.5 : 1)}
          stroke-dasharray={lp.kind === 'void'
            ? `${view.stroke * 6} ${view.stroke * 3}`
            : undefined}
        />
      {/each}
    </svg>
    <figcaption>
      <small>
        {section.plane_axes[0]}
        {view.umin.toFixed(1)}…{view.umax.toFixed(1)} ({view.w.toFixed(1)}
        mm) · {section.plane_axes[1]}
        {view.vmin.toFixed(1)}…{view.vmax.toFixed(1)} ({view.h.toFixed(1)} mm)
      </small>
    </figcaption>
  </figure>
{:else}
  <p><small>The plane misses the model.</small></p>
{/if}

<style>
  figure {
    margin: 0;
  }
  svg {
    width: 100%;
    max-height: 14rem;
    background: var(--pico-background-color);
    border: 1px solid var(--pico-muted-border-color);
  }
  .fill {
    fill: color-mix(in srgb, var(--pico-primary) 25%, transparent);
    stroke: none;
  }
  .outer {
    fill: none;
    stroke: var(--pico-primary);
  }
  .void {
    fill: none;
    stroke: #d9534f;
  }
</style>
