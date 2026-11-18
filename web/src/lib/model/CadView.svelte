<script lang="ts">
  // three-cad-viewer embed. The library (~450 kB gzipped) is imported on first render,
  // so it lands in its own chunk and only loads when the Model tab shows a model.
  import { onDestroy, onMount } from 'svelte';
  import type {
    ChangeNotification,
    ClipIndex,
    Display,
    Shapes,
    Vector3Tuple,
    Viewer,
  } from 'three-cad-viewer';

  type Lib = typeof import('three-cad-viewer');

  interface Props {
    /** Tessellation from the API (ocp-tessellate instanced format) or decoded Shapes. */
    data: unknown;
    /** A clip slider moved in the viewer's own UI: plane `index` now at world `offset`. */
    onclip?: (index: ClipIndex, offset: number) => void;
    /** Rendered: stats for the status line. */
    onrendered?: (info: { triangles: number; ms: number; version: string }) => void;
  }

  let { data, onclip, onrendered }: Props = $props();

  const TREE_WIDTH = 240;
  const TOOLBAR = 48; // the viewer's toolbar sits above the canvas
  let host: HTMLDivElement;
  let mount: HTMLDivElement | null = null; // inside host's shadow root
  let lib: Lib | null = null;
  let lastUserInput = -Infinity;
  let display: Display | null = null;
  let viewer: Viewer | null = null;
  let shapes: Shapes | null = null;
  let error = $state('');
  let loading = $state(false);
  let observer: ResizeObserver | null = null;
  let frame = 0;

  /**
   * The viewer lives in a shadow root with its own stylesheet: Pico's global element
   * styles (inputs, buttons, tables) would otherwise restyle the viewer's UI.
   */
  async function loadLib(): Promise<Lib> {
    if (!lib) {
      const [mod, css] = await Promise.all([
        import('three-cad-viewer'),
        import('three-cad-viewer/css?inline'),
      ]);
      const root = host.shadowRoot ?? host.attachShadow({ mode: 'open' });
      const style = document.createElement('style');
      style.textContent = `${css.default}\n:host{display:block}`;
      root.append(style);
      lib = mod;
    }
    return lib;
  }

  const GLASS_BELOW = 720; // narrow hosts: the tree floats over the canvas (glass mode)

  function size(): { cadWidth: number; height: number; glass: boolean } {
    const r = host.getBoundingClientRect();
    const glass = r.width < GLASS_BELOW;
    return {
      glass,
      cadWidth: Math.max(200, Math.floor(r.width - (glass ? 0 : TREE_WIDTH) - 4)),
      height: Math.max(240, Math.floor(r.height - TOOLBAR)),
    };
  }

  function dispose() {
    viewer?.dispose();
    viewer = null;
    display = null;
    mount?.remove();
    mount = null;
  }

  // Follow clip sliders only when the user moved them: the viewer also reports the values
  // it sets itself (on render, on tab changes, and for our own clip() calls).
  function notify(change: ChangeNotification) {
    if (!onclip || !viewer || performance.now() - lastUserInput > 1500) return;
    for (const i of [0, 1, 2] as ClipIndex[]) {
      const c = change[`clip_slider_${i}`];
      if (c && typeof c.new === 'number') onclip(i, center()[i] + c.new);
    }
  }

  async function show(input: unknown) {
    if (!input || !host) return;
    loading = true;
    error = '';
    try {
      const t0 = performance.now();
      const L = await loadLib();
      shapes = L.isInstancedFormat(input) ? L.decodeInstancedFormat(input) : (input as Shapes);
      dispose();
      // Gotcha (spike #58): the canvas size comes from the Viewer's options as well as the
      // Display's, so both get cadWidth/height/treeWidth.
      const opts = {
        ...size(),
        treeWidth: TREE_WIDTH,
        theme: 'browser' as const,
        pinning: false,
        tools: true,
        measureTools: true,
        selectTool: true,
        explodeTool: false,
        zscaleTool: false,
        zebraTool: false,
        externalMeasurementBackend: false,
        studioTool: false,
      };
      mount = document.createElement('div');
      host.shadowRoot!.append(mount);
      display = new L.Display(mount, opts);
      viewer = new L.Viewer(display, opts, notify);
      viewer.render(
        shapes,
        { edgeColor: 0x707070, metalness: 0.3, roughness: 0.65, normalLen: 0 },
        {
          up: 'Z',
          ortho: true,
          axes: false,
          grid: [false, false, false],
          // Per-plane cap colours make internal voids stand out (spike #58).
          clipObjectColors: false,
          clipIntersection: false,
          clipPlaneHelpers: false,
        },
      );
      onrendered?.({
        triangles: countTriangles(shapes),
        ms: performance.now() - t0,
        version: L.version,
      });
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  function countTriangles(s: Shapes): number {
    const own = s.shape && 'triangles' in s.shape ? (s.shape.triangles?.length ?? 0) / 3 : 0;
    return (s.parts ?? []).reduce((n, p) => n + countTriangles(p), own);
  }

  /** Centre of the scene's bbox: clip slider values are distances from it. */
  function center(): Vector3Tuple {
    const c = viewer?.bbox?.center();
    if (c) return c;
    const bb = shapes?.bb;
    return bb
      ? [(bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2, (bb.zmin + bb.zmax) / 2]
      : [0, 0, 0];
  }

  function halfSpan(i: number): number {
    const bb = shapes?.bb;
    if (!bb) return 1000;
    const k = (['x', 'y', 'z'] as const)[i];
    return (bb[`${k}max`] - bb[`${k}min`]) / 2;
  }

  /**
   * Show a single clip plane at world `offset` along `axis` (null: no clipping).
   * Default normals are −X/−Y/−Z, so the low side is kept and the plane sits at
   * centre + slider value; the other two planes move past the far side.
   */
  export function clip(axis: ClipIndex | null, offset = 0) {
    if (!viewer) return;
    if (axis === null) {
      viewer.setActiveTab('tree');
      return;
    }
    viewer.setActiveTab('clip');
    const c = center();
    for (const i of [0, 1, 2] as ClipIndex[]) {
      let value = i === axis ? offset - c[i] : halfSpan(i) + 1;
      if (value === -1) value = -1 + 1e-6; // -1 is the viewer's "unset" sentinel
      viewer.setClipSlider(i, value, true);
    }
  }

  export function preset(dir: 'iso' | 'front' | 'rear' | 'left' | 'right' | 'top' | 'bottom') {
    viewer?.presetCamera(dir);
  }

  $effect(() => {
    show(data);
  });

  onMount(() => {
    observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (!viewer) return;
        const { cadWidth, height, glass } = size();
        viewer.resizeCadView(cadWidth, TREE_WIDTH, height, glass);
      });
    });
    observer.observe(host);
    const touched = () => (lastUserInput = performance.now());
    for (const ev of ['pointerdown', 'input', 'keydown', 'wheel'])
      host.addEventListener(ev, touched, { capture: true, passive: true });
  });

  onDestroy(() => {
    observer?.disconnect();
    cancelAnimationFrame(frame);
    dispose();
  });
</script>

<div class="cad-wrap">
  <div class="cad" bind:this={host}></div>
  {#if loading}
    <div class="cad-msg" aria-busy="true">Loading viewer…</div>
  {:else if error}
    <div class="cad-msg"><small>Viewer error: {error}</small></div>
  {/if}
</div>

<style>
  .cad-wrap {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 0;
  }
  .cad {
    width: 100%;
    height: 100%;
    overflow: hidden;
  }
  .cad-msg {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    pointer-events: none;
  }
</style>
