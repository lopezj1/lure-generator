/**
 * CNC preview data prep, independent of three.js (runs in Node for tests).
 *
 * A **job** is one or more CNC toolpaths played back to back: a build's `fab/cnc/` holds one
 * file per tool by default, or a single program with `M6` changes. Tools are merged across
 * files by T number, so a tool keeps its color whichever file it is in. Each file's job
 * timeline (the encoder's `time` array: start/end seconds per segment, tool changes and
 * dwells included) is shifted by the files before it.
 *
 * GPU buffers follow the print preview's trick (see `print/model.ts`): segment s is the
 * index pair (s+1, s), so `flat` attributes at vertex s belong to segment s.
 */

import type { Bounds, CncToolpath, ToolEntry, Vec3 } from '../toolpath/reader';

export const KIND_JUMP = 0;
export const KIND_RAPID = 1;
export const KIND_FEED = 2;
export const KIND_PLUNGE = 3;

export interface CncPart {
  label: string;
  tp: CncToolpath;
  /** Job time (s) at the start of this file. */
  offset: number;
  /** Length of this file's timeline (s), waits included. */
  duration: number;
  /** Start/end time per segment, local to the file (2 per segment). */
  times: Float32Array;
  /** Local tool index → job tool index. */
  toolMap: Uint8Array;
}

export interface JobTool {
  /** `T<number>`, or `<file>:<index>` for a file that never selects a tool. */
  key: string;
  number: number | null;
  entry: ToolEntry;
  segments: number;
  cutLength: number;
  rapidLength: number;
  cutTime: number;
  rapidTime: number;
  /** Motion time (cut + rapid). */
  time: number;
  files: string[];
}

export interface JobMarker {
  type: 'tool_change' | 'pause' | 'file';
  time: number;
  part: number;
  segment: number;
  line: number;
  /** Job tool index (tool changes and file starts). */
  tool?: number;
  title: string;
}

export interface CncJob {
  parts: CncPart[];
  tools: JobTool[];
  duration: number;
  motionTime: number;
  waitTime: number;
  toolChanges: number;
  segments: number;
  bounds: Bounds | null;
  /** Feed and plunge moves only (camera framing, bbox dimensions). */
  active: Bounds | null;
  /** From Carbide-style `(stockMin:…)` / `(stockMax:…)` comments, merged over files. */
  stock: Bounds | null;
  markers: JobMarker[];
}

function union(a: Bounds | null, b: Bounds | null | undefined): Bounds | null {
  if (!b) return a;
  if (!a) return { min: [...b.min] as Vec3, max: [...b.max] as Vec3 };
  return {
    min: a.min.map((v, k) => Math.min(v, b.min[k])) as Vec3,
    max: a.max.map((v, k) => Math.max(v, b.max[k])) as Vec3,
  };
}

/** Per-segment start/end times: the encoder's, or one second per drawn move for old files. */
function segmentTimes(tp: CncToolpath): Float32Array {
  if (tp.time) return tp.time;
  const out = new Float32Array(2 * tp.segments);
  let t = 0;
  for (let s = 0; s < tp.segments; s++) {
    out[2 * s] = t;
    if (tp.kind[s] !== KIND_JUMP) t += 1;
    out[2 * s + 1] = t;
  }
  return out;
}

function toolKey(entry: ToolEntry, part: number, index: number): string {
  return entry.number != null ? `T${entry.number}` : `${part}:${index}`;
}

/** Natural file order (`op2` before `op10`). */
export function compareNames(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}

export function buildJob(files: { label: string; tp: CncToolpath }[]): CncJob {
  const tools: JobTool[] = [];
  const byKey = new Map<string, number>();
  const parts: CncPart[] = [];
  const markers: JobMarker[] = [];
  let offset = 0;
  let bounds: Bounds | null = null;
  let active: Bounds | null = null;
  let stock: Bounds | null = null;
  let motionTime = 0;
  let waitTime = 0;
  let toolChanges = 0;
  let segments = 0;

  files.forEach(({ label, tp }, p) => {
    const h = tp.header;
    const toolMap = new Uint8Array(Math.max(1, h.tools.length));
    h.tools.forEach((entry, i) => {
      const key = toolKey(entry, p, i);
      let g = byKey.get(key);
      if (g === undefined) {
        g = tools.length;
        byKey.set(key, g);
        tools.push({
          key,
          number: entry.number,
          entry: { ...entry },
          segments: 0,
          cutLength: 0,
          rapidLength: 0,
          cutTime: 0,
          rapidTime: 0,
          time: 0,
          files: [],
        });
      } else {
        tools[g].entry = { ...entry, ...tools[g].entry };
      }
      toolMap[i] = Math.min(g, 255);
      const t = tools[g];
      const st = h.stats.tools[i];
      if (st) {
        t.segments += st.segments;
        t.cutLength += st.active_length_mm;
        t.rapidLength += st.rapid_length_mm;
        t.cutTime += st.cut_time_s ?? 0;
        t.rapidTime += st.rapid_time_s ?? 0;
        t.time += st.time_s;
      }
      if ((st?.segments ?? 0) > 0 && !t.files.includes(label)) t.files.push(label);
    });

    const times = segmentTimes(tp);
    const n = tp.segments;
    const lastEnd = n ? times[2 * n - 1] : 0;
    const duration = Math.max(h.stats.time_s ?? 0, lastEnd);
    parts.push({ label, tp, offset, duration, times, toolMap });

    if (files.length > 1) {
      const first = n ? toolMap[tp.tool[0]] : undefined;
      markers.push({
        type: 'file',
        time: offset,
        part: p,
        segment: 0,
        line: 1,
        tool: first,
        title: `${label}${first !== undefined ? ` (${toolLabel(tools[first])})` : ''}`,
      });
    }
    for (const m of h.markers) {
      const local = m.time_s ?? (m.segment > 0 ? times[2 * m.segment - 1] : 0);
      if (m.type === 'tool_change') {
        const tool = toolMap[m.tool];
        markers.push({
          type: 'tool_change',
          time: offset + local,
          part: p,
          segment: m.segment,
          line: m.line,
          tool,
          title: `Tool change → ${toolLabel(tools[tool])} (line ${m.line})`,
        });
      } else {
        markers.push({
          type: 'pause',
          time: offset + local,
          part: p,
          segment: m.segment,
          line: m.line,
          title: `${m.command} (line ${m.line})`,
        });
      }
    }

    bounds = union(bounds, h.bounds);
    active = union(active, h.bounds_active ?? h.bounds);
    stock = union(stock, h.stock);
    motionTime += h.stats.motion_s ?? h.stats.time_s ?? 0;
    waitTime += h.stats.wait_s ?? 0;
    toolChanges += h.stats.tool_changes ?? h.markers.filter((m) => m.type === 'tool_change').length;
    segments += n;
    offset += duration;
  });

  return {
    parts,
    tools,
    duration: offset,
    motionTime,
    waitTime,
    toolChanges,
    segments,
    bounds,
    active,
    stock,
    markers,
  };
}

export function toolLabel(t: JobTool | undefined): string {
  if (!t) return '?';
  const name = t.number != null ? `T${t.number}` : 'no T';
  const d = t.entry.diameter;
  return d ? `${name} ⌀${d}` : name;
}

export interface PartBuffers {
  /** 2 per drawn segment: (s+1, s); jumps are left out. */
  index: Uint32Array;
  /** 4 per point: kind, job tool, 0, 0 (segment s at vertex s; the last is padding). */
  attrs: Uint8Array;
  /** Cumulative drawn length (mm) at each point; dashes and in-segment progress use it. */
  dist: Float32Array;
  /** 4 per point: job-time start, end, length of segment s, 0. */
  seg: Float32Array;
}

export function buildPartBuffers(part: CncPart): PartBuffers {
  const { tp, times, toolMap, offset } = part;
  const n = tp.segments;
  const p = tp.points;
  let drawn = 0;
  for (let s = 0; s < n; s++) if (tp.kind[s] !== KIND_JUMP) drawn++;
  const index = new Uint32Array(2 * drawn);
  const attrs = new Uint8Array(4 * (n + 1));
  const dist = new Float32Array(n + 1);
  const seg = new Float32Array(4 * (n + 1));
  let d = 0;
  let k = 0;
  for (let s = 0; s < n; s++) {
    const kind = tp.kind[s];
    const dx = p[3 * s + 3] - p[3 * s];
    const dy = p[3 * s + 4] - p[3 * s + 1];
    const dz = p[3 * s + 5] - p[3 * s + 2];
    const len = kind === KIND_JUMP ? 0 : Math.sqrt(dx * dx + dy * dy + dz * dz);
    dist[s] = d;
    d += len;
    attrs[4 * s] = kind;
    attrs[4 * s + 1] = toolMap[tp.tool[s]] ?? 0;
    seg[4 * s] = offset + times[2 * s];
    seg[4 * s + 1] = offset + times[2 * s + 1];
    seg[4 * s + 2] = len;
    if (kind !== KIND_JUMP) {
      index[k++] = s + 1;
      index[k++] = s;
    }
  }
  dist[n] = d;
  return { index, attrs, dist, seg };
}

export interface Playhead {
  part: number;
  /** The segment running (or about to start) at this time; -1 before an empty file. */
  segment: number;
  /** Tool tip position. */
  pos: Vec3;
  /** True during a tool change or dwell (before `segment` starts). */
  waiting: boolean;
  /** Job tool index loaded at this time. */
  tool: number;
}

/** First segment whose end time ≥ `t` (local seconds), or `n` when `t` is past the end. */
export function segmentAt(times: Float32Array, n: number, t: number): number {
  let lo = 0;
  let hi = n;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[2 * mid + 1] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Where the tool is at job time `t`. */
export function playheadAt(job: CncJob, t: number): Playhead | null {
  const { parts } = job;
  if (!parts.length) return null;
  let p = parts.length - 1;
  for (let i = 0; i < parts.length; i++) {
    if (t < parts[i].offset + parts[i].duration) {
      p = i;
      break;
    }
  }
  const part = parts[p];
  const { tp, times } = part;
  const n = tp.segments;
  if (!n) return { part: p, segment: -1, pos: [0, 0, 0], waiting: true, tool: part.toolMap[0] };
  const local = t - part.offset;
  const pts = tp.points;
  let s = segmentAt(times, n, local);
  if (s >= n) {
    s = n - 1;
    const i = 3 * n;
    const pos: Vec3 = [pts[i], pts[i + 1], pts[i + 2]];
    return { part: p, segment: s, pos, waiting: false, tool: part.toolMap[tp.tool[s]] };
  }
  // Skip jumps: the tool reappears at the next drawn move.
  while (s < n - 1 && tp.kind[s] === KIND_JUMP && times[2 * s + 1] <= local) s++;
  const t0 = times[2 * s];
  const t1 = times[2 * s + 1];
  const waiting = local < t0;
  const f = waiting || t1 <= t0 ? (local >= t1 ? 1 : 0) : (local - t0) / (t1 - t0);
  const a = 3 * s;
  const pos: Vec3 = [
    pts[a] + (pts[a + 3] - pts[a]) * f,
    pts[a + 1] + (pts[a + 4] - pts[a + 1]) * f,
    pts[a + 2] + (pts[a + 5] - pts[a + 2]) * f,
  ];
  return { part: p, segment: s, pos, waiting, tool: part.toolMap[tp.tool[s]] };
}

/**
 * The job time after stepping `steps` drawn moves from `t`: forward lands on a move's end,
 * backward on a move's start, like stepping through G-code lines.
 */
export function stepTime(job: CncJob, t: number, steps: number): number {
  const at = playheadAt(job, t);
  if (!at) return t;
  // Walk across files when needed.
  let p = at.part;
  let s = at.segment;
  let left = Math.abs(steps);
  const forward = steps > 0;
  let time = t;
  while (left > 0) {
    const part = job.parts[p];
    const n = part.tp.segments;
    if (forward) {
      if (s >= n) {
        if (p === job.parts.length - 1) return job.duration;
        p++;
        s = 0;
        continue;
      }
      const end = part.offset + part.times[2 * s + 1];
      if (part.tp.kind[s] !== KIND_JUMP && end > t + 1e-6) {
        time = end;
        left--;
      }
      s++;
    } else {
      if (s < 0) {
        if (p === 0) return 0;
        p--;
        s = job.parts[p].tp.segments - 1;
        continue;
      }
      const start = part.offset + part.times[2 * s];
      if (part.tp.kind[s] !== KIND_JUMP && start < t - 1e-6) {
        time = start;
        left--;
      }
      s--;
    }
  }
  return Math.max(0, Math.min(job.duration, time));
}

export function formatClock(seconds: number): string {
  if (!Number.isFinite(seconds)) return '–';
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

export function formatLength(mm: number): string {
  return mm >= 1000 ? `${(mm / 1000).toFixed(2)} m` : `${mm.toFixed(0)} mm`;
}
