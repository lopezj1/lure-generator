/**
 * Print preview data prep, independent of three.js (runs in Node for timing/tests).
 *
 * The GPU draws the file's polyline as indexed line segments that share the `points` array
 * as the position buffer (no copy). Segment s is the index pair (s+1, s): WebGL2 takes
 * `flat` varyings from the last ("provoking") vertex of a primitive, which is vertex s, so a
 * per-vertex attribute holding segment s's kind/feature/tool colors exactly that segment.
 * Draw ranges are then plain segment ranges: segments [a, b) are indices [2a, 2b).
 */

import type { PrintToolpath } from '../toolpath/reader';

export interface PrintBuffers {
  /** 2 per segment: (s+1, s). */
  index: Uint32Array;
  /** 4 per point: kind, feature, tool, 0 (for segment s at vertex s; the last is padding). */
  attrs: Uint8Array;
}

export function buildPrintBuffers(tp: PrintToolpath): PrintBuffers {
  const n = tp.segments;
  const index = new Uint32Array(2 * n);
  const attrs = new Uint8Array(4 * (n + 1));
  const { kind, feature, tool } = tp;
  for (let s = 0; s < n; s++) {
    index[2 * s] = s + 1;
    index[2 * s + 1] = s;
    const a = 4 * s;
    attrs[a] = kind[s];
    attrs[a + 1] = feature[s];
    attrs[a + 2] = tool[s];
  }
  return { index, attrs };
}

export interface LayerTick {
  layer: number;
  type: 'tool_change' | 'pause';
  /** Target tool for a tool change. */
  tool?: number;
  title: string;
}

/** One tick per layer and marker type (several tool changes in a layer collapse). */
export function layerTicks(tp: PrintToolpath): LayerTick[] {
  const seen = new Map<string, LayerTick>();
  for (const m of tp.header.markers) {
    const layer = m.layer ?? 0;
    const key = `${m.type}:${layer}`;
    const prev = seen.get(key);
    if (m.type === 'pause') {
      if (!prev) seen.set(key, { layer, type: 'pause', title: `${m.command} (line ${m.line})` });
    } else {
      const label = `T${tp.header.tools[m.tool]?.number ?? m.tool}`;
      if (prev) prev.title += `, ${label}`;
      else seen.set(key, { layer, type: 'tool_change', tool: m.tool, title: `→ ${label}` });
    }
  }
  return [...seen.values()];
}

/** Segments drawn for layers `lo…hi` with `moves` segments of layer `hi`: [start, end). */
export function drawRange(offsets: readonly number[], lo: number, hi: number, moves: number) {
  const start = offsets[lo];
  const end = Math.min(offsets[hi] + moves, offsets[hi + 1]);
  return { start, end };
}

/** Feature index of "Prime tower" (or a wipe tower), or -1. */
export function primeTowerFeature(features: readonly string[]): number {
  return features.findIndex((f) => /prime tower|wipe tower/i.test(f));
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '–';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m ${String(s % 60).padStart(2, '0')}s`;
}
