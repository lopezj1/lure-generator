<script lang="ts">
  import { onMount } from "svelte";
  import { Display, Viewer, version } from "three-cad-viewer";
  import type { Shapes, ThemeInput, ClipIndex } from "three-cad-viewer";
  import "three-cad-viewer/css";

  const MODELS = ["scene", "loft3d", "profile25d", "cavity", "loft3d_brepmesh", "cavity_brepmesh"] as const;
  type Model = (typeof MODELS)[number];
  const AXES = ["X", "Y", "Z"] as const;

  let model: Model = $state("cavity");
  let theme: ThemeInput = $state("light");
  let clipOn = $state([true, false, false]);
  let clip = $state([0, 0, 0]); // mm offsets from the bbox centre, along +axis
  let bb = $state({ xmin: 0, xmax: 1, ymin: 0, ymax: 1, zmin: 0, zmax: 1 });
  let status = $state("");
  let container: HTMLDivElement;
  let display: Display | null = null;
  let viewer: Viewer | null = null;

  const range = (i: number) => {
    const k = "xyz"[i];
    const lo = (bb as any)[`${k}min`], hi = (bb as any)[`${k}max`];
    return { lo, hi, mid: (lo + hi) / 2 };
  };

  async function load(name: Model) {
    const t0 = performance.now();
    const shapes: Shapes = await (await fetch(`models/${name}.json`)).json();
    const t1 = performance.now();
    viewer?.dispose();
    display?.dispose();
    container.innerHTML = "";
    const opts = {
      cadWidth: Math.max(600, window.innerWidth - 300),
      height: Math.max(500, window.innerHeight - 140),
      treeWidth: 240,
      theme: theme as any,
      pinning: false,
      measureTools: true,
      selectTool: true,
      explodeTool: false,
    };
    display = new Display(container, opts as any);
    // gotcha: the canvas size comes from the Viewer's options, not only the Display's
    viewer = new Viewer(display, opts as any, () => {});
    viewer.render(shapes, { edgeColor: 0x707070, metalness: 0.3, roughness: 0.65, normalLen: 0 }, {
      up: "Z", ortho: true, axes: false, grid: [false, false, false],
      clipObjectColors: false,
    });
    bb = (shapes as any).bb;
    const t2 = performance.now();
    const tris = countTris(shapes);
    status = `three-cad-viewer ${version} · ${name}: ${tris} triangles · fetch+parse ${(t1 - t0).toFixed(0)} ms · render ${(t2 - t1).toFixed(0)} ms`;
    (window as any).viewer = viewer;
    (window as any).ready = true;
    applyClip();
  }

  function countTris(s: any): number {
    return (s.parts ?? []).reduce((n: number, p: any) => n + countTris(p), 0) +
      (s.shape?.triangles ? s.shape.triangles.length / 3 : 0);
  }

  // clip plane i has normal -axis (viewer default): it keeps points with axis-coord < offset.
  // The viewer's slider value is the plane's distance from the bbox centre along its normal.
  function applyClip() {
    if (!viewer) return;
    const anyOn = clipOn.some(Boolean);
    viewer.setActiveTab(anyOn ? "clip" : "tree");
    for (const i of [0, 1, 2] as ClipIndex[]) {
      const { lo, hi, mid } = range(i);
      const span = (hi - lo) / 2 + 1;
      const off = clipOn[i] ? clip[i] : span; // "off" = plane beyond the far side
      viewer.setClipSlider(i, off - (i === 0 ? 0 : 0) + 0 * mid);
    }
  }

  function setTheme(t: ThemeInput) {
    theme = t;
    viewer?.setTheme(t);
    document.documentElement.dataset.theme = t;
  }

  onMount(() => {
    load(model);
    (window as any).app = { load, setClip: (i: number, on: boolean, v: number) => { clipOn[i] = on; clip[i] = v; applyClip(); }, setTheme, preset: (d: any, z?: number) => viewer?.presetCamera(d, z ?? null) };
  });
</script>

<header>
  <label>Model
    <select bind:value={model} onchange={() => load(model)}>
      {#each MODELS as m}<option value={m}>{m}</option>{/each}
    </select>
  </label>
  {#each AXES as ax, i}
    <label class="clip">
      <input type="checkbox" bind:checked={clipOn[i]} onchange={applyClip} /> {ax}
      <input type="range" min={-((range(i).hi - range(i).lo) / 2)} max={(range(i).hi - range(i).lo) / 2}
        step="0.5" bind:value={clip[i]} oninput={applyClip} disabled={!clipOn[i]} />
      <code>{clip[i].toFixed(1)}</code>
    </label>
  {/each}
  {#each ["iso", "front", "top", "left"] as d}
    <button onclick={() => viewer?.presetCamera(d as any)}>{d}</button>
  {/each}
  <button onclick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? "light" : "dark"}</button>
</header>
<div bind:this={container} class="cad"></div>
<footer>{status}</footer>

<style>
  :global(html[data-theme="dark"] body) { background: #1e1e1e; color: #ddd; }
  :global(body) { margin: 8px; font: 13px system-ui, sans-serif; }
  header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 6px; }
  .clip { display: flex; gap: 4px; align-items: center; }
  footer { margin-top: 4px; opacity: 0.7; }
</style>
