<script lang="ts">
  /**
   * One- or two-thumb integer slider with ticks. Vertical sliders grow upward.
   * A focused thumb handles the arrow keys along its own axis (plus PageUp/PageDown,
   * Home/End); keys along the other axis bubble up to the viewer's shortcuts.
   */
  export interface Tick {
    value: number;
    color: string;
    title: string;
    wide?: boolean;
  }

  interface Props {
    min: number;
    max: number;
    /** One value, or [low, high]. */
    values: number[];
    vertical?: boolean;
    ticks?: Tick[];
    label: string;
    format?: (v: number) => string;
    onchange: (values: number[]) => void;
  }

  let {
    min,
    max,
    values,
    vertical = false,
    ticks = [],
    label,
    format = String,
    onchange,
  }: Props = $props();

  let track: HTMLDivElement | undefined = $state();
  let dragging = -1;

  const span = $derived(Math.max(1, max - min));
  const pct = (v: number) => ((v - min) / span) * 100;

  function clampAt(i: number, v: number): number {
    let lo = min;
    let hi = max;
    if (values.length === 2) {
      if (i === 0) hi = values[1];
      else lo = values[0];
    }
    return Math.max(lo, Math.min(hi, Math.round(v)));
  }

  function set(i: number, v: number) {
    const next = [...values];
    next[i] = clampAt(i, v);
    if (next[i] !== values[i]) onchange(next);
  }

  function valueAt(e: PointerEvent): number {
    const r = track!.getBoundingClientRect();
    const f = vertical ? (r.bottom - e.clientY) / r.height : (e.clientX - r.left) / r.width;
    return min + Math.max(0, Math.min(1, f)) * span;
  }

  function down(e: PointerEvent, i: number) {
    e.preventDefault();
    e.stopPropagation();
    dragging = i;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    (e.currentTarget as HTMLElement).focus();
  }

  function trackDown(e: PointerEvent) {
    const v = valueAt(e);
    const i =
      values.length === 2 && Math.abs(v - values[0]) < Math.abs(v - values[1])
        ? 0
        : values.length - 1;
    set(i, v);
  }

  function move(e: PointerEvent, i: number) {
    if (dragging === i) set(i, valueAt(e));
  }

  function key(e: KeyboardEvent, i: number) {
    const inc = vertical ? 'ArrowUp' : 'ArrowRight';
    const dec = vertical ? 'ArrowDown' : 'ArrowLeft';
    const steps: Record<string, number> = { [inc]: 1, [dec]: -1, PageUp: 10, PageDown: -10 };
    let v: number | undefined;
    if (e.key in steps) v = values[i] + steps[e.key] * (e.shiftKey ? 10 : 1);
    else if (e.key === 'Home') v = min;
    else if (e.key === 'End') v = max;
    if (v === undefined) return;
    e.preventDefault();
    e.stopPropagation();
    set(i, v);
  }
</script>

<div class="slider" class:vertical>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="track" bind:this={track} onpointerdown={trackDown}>
    {#if values.length === 2}
      <div
        class="fill"
        style={vertical
          ? `bottom:${pct(values[0])}%;height:${pct(values[1]) - pct(values[0])}%`
          : `left:${pct(values[0])}%;width:${pct(values[1]) - pct(values[0])}%`}
      ></div>
    {:else}
      <div
        class="fill"
        style={vertical ? `bottom:0;height:${pct(values[0])}%` : `left:0;width:${pct(values[0])}%`}
      ></div>
    {/if}
    {#each ticks as t, k (k)}
      <div
        class="tick"
        class:wide={t.wide}
        title={t.title}
        style={`${vertical ? 'bottom' : 'left'}:${pct(t.value)}%;background:${t.color}`}
      ></div>
    {/each}
    {#each values as v, i (i)}
      <div
        class="thumb"
        role="slider"
        tabindex="0"
        aria-label={values.length === 2 ? `${label} ${i ? 'top' : 'bottom'}` : label}
        aria-orientation={vertical ? 'vertical' : 'horizontal'}
        aria-valuemin={min}
        aria-valuemax={max}
        aria-valuenow={v}
        style={`${vertical ? 'bottom' : 'left'}:${pct(v)}%`}
        onpointerdown={(e) => down(e, i)}
        onpointermove={(e) => move(e, i)}
        onpointerup={() => (dragging = -1)}
        onkeydown={(e) => key(e, i)}
      >
        <span class="bubble">{format(v)}</span>
      </div>
    {/each}
  </div>
</div>

<style>
  .slider {
    position: relative;
    padding: 0.6rem 0.8rem;
    user-select: none;
    touch-action: none;
  }
  .track {
    position: relative;
    height: 6px;
    border-radius: 3px;
    background: rgb(255 255 255 / 0.18);
    cursor: pointer;
  }
  .vertical {
    height: 100%;
    padding: 0.8rem 0.6rem;
  }
  .vertical .track {
    height: 100%;
    width: 6px;
  }
  .fill {
    position: absolute;
    top: 0;
    bottom: 0;
    background: var(--pico-primary-background, #3b82f6);
    border-radius: 3px;
  }
  .vertical .fill {
    top: auto;
    left: 0;
    right: 0;
  }
  .tick {
    position: absolute;
    top: -5px;
    width: 2px;
    height: 16px;
    transform: translateX(-1px);
    pointer-events: auto;
  }
  .vertical .tick {
    top: auto;
    left: -6px;
    width: 18px;
    height: 2px;
    transform: translateY(1px);
  }
  .tick.wide {
    width: 4px;
  }
  .vertical .tick.wide {
    width: 22px;
    left: -8px;
    height: 4px;
  }
  .thumb {
    position: absolute;
    top: 50%;
    width: 16px;
    height: 16px;
    margin: -8px 0 0 -8px;
    border-radius: 50%;
    background: #fff;
    border: 2px solid var(--pico-primary-background, #3b82f6);
    cursor: grab;
    z-index: 1;
  }
  .vertical .thumb {
    top: auto;
    left: 50%;
    margin: 0 0 -8px -8px;
  }
  .thumb:focus-visible {
    outline: 2px solid #ffd54a;
  }
  .bubble {
    position: absolute;
    bottom: 18px;
    left: 50%;
    transform: translateX(-50%);
    font-size: 0.7rem;
    white-space: nowrap;
    padding: 0 0.3rem;
    border-radius: 0.25rem;
    background: rgb(0 0 0 / 0.65);
    color: #fff;
    pointer-events: none;
  }
  .vertical .bubble {
    bottom: auto;
    left: auto;
    right: 20px;
    top: 50%;
    transform: translateY(-50%);
  }
</style>
