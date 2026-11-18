/**
 * Reader for the viewer toolpath binary format v1 (docs/viewer-binary-format.md).
 *
 * Shared by the print (W6) and CNC (W7) previews. Arrays are typed-array *views* of the
 * buffer (no copies). The module has no imports so Node can run it directly with type
 * stripping (`web/tests/toolpath-reader.test.ts`).
 */

export const MAGIC = 'LGTP';
export const VERSION = 1;
const PREFIX_BYTES = 12;

export class ToolpathFormatError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ToolpathFormatError';
  }
}

export type Dtype = 'float32' | 'uint8' | 'uint16' | 'uint32';
export type Vec3 = [number, number, number];
export interface Bounds {
  min: Vec3;
  max: Vec3;
}

export interface ArrayEntry {
  name: string;
  dtype: Dtype;
  /** Byte offset from the start of the file. */
  offset: number;
  count: number;
  components: number;
}

export interface ToolChangeMarker {
  type: 'tool_change';
  line: number;
  /** First segment after the event; draw the marker at `points[segment]`. */
  segment: number;
  from: number | null;
  tool: number;
  /** Print only. */
  layer?: number;
}

export interface PauseMarker {
  type: 'pause';
  line: number;
  segment: number;
  command: string;
  /** Print only. */
  layer?: number;
}

export type Marker = ToolChangeMarker | PauseMarker;

export interface ToolEntry {
  number: number | null;
  /** Print: `filament_colour` as `#RRGGBB`. */
  color?: string;
  filament_type?: string;
  filament?: string;
  /** Print: net E per tool in mm. */
  filament_mm?: number;
  /** CNC: from Fusion or Carbide tool comments. */
  diameter?: number;
  corner_radius?: number;
  zmin?: number;
  description?: string;
}

export interface LayerEntry {
  z: number | null;
  height: number | null;
  /** First segment of the layer. */
  start: number;
  line: number;
}

export interface LengthStat {
  segments: number;
  length_mm: number;
}

export interface ToolStat {
  tool: number;
  number: number | null;
  segments: number;
  active_length_mm: number;
  rapid_length_mm: number;
  time_s: number;
}

export interface ToolpathHeader {
  format: 'luregen-toolpath';
  version: number;
  kind: 'print' | 'cnc';
  segments: number;
  kinds: string[];
  tools: ToolEntry[];
  markers: Marker[];
  bounds: Bounds | null;
  bounds_active: Bounds | null;
  stats: {
    kinds: Record<string, LengthStat>;
    tools: ToolStat[];
    time_s: number;
    features?: Record<string, LengthStat>;
  };
  source: {
    name?: string;
    /** Print */
    generator?: string;
    header_layers?: number | null;
    tool_changes?: number | null;
    estimated_time?: string | null;
    estimated_time_s?: number | null;
    /** CNC */
    skipped_lines?: number[];
    skipped_count?: number;
  };
  params: Record<string, unknown>;
  arrays: ArrayEntry[];
  /** Print only. */
  features?: string[];
  layers?: LayerEntry[];
  layer_offsets?: number[];
  layer_count?: number;
  /** CNC only. */
  clearance_z?: number;
  stock?: Bounds;
}

/** Common to print and CNC. Segment `i` runs `points[3i..3i+2]` → `points[3i+3..3i+5]`. */
interface ToolpathBase {
  header: ToolpathHeader;
  /** Number of segments N (`points` has N+1 xyz triplets). */
  segments: number;
  points: Float32Array;
  /** Index into `header.kinds`; 0 = jump (never drawn). */
  kind: Uint8Array;
  tool: Uint8Array;
  feed: Float32Array;
  /** 1-based source line. */
  line: Uint32Array;
}

export interface PrintToolpath extends ToolpathBase {
  type: 'print';
  feature: Uint8Array;
  layer: Uint16Array;
  /** µm, 0 when not extruding. */
  width: Uint16Array;
  /** µm. */
  height: Uint16Array;
}

export interface CncToolpath extends ToolpathBase {
  type: 'cnc';
}

export type Toolpath = PrintToolpath | CncToolpath;

const CTORS = {
  float32: Float32Array,
  uint8: Uint8Array,
  uint16: Uint16Array,
  uint32: Uint32Array,
} as const;

/** The buffer and the file's start within it (copies only when misaligned). */
function asAligned(data: ArrayBuffer | ArrayBufferView): { buffer: ArrayBuffer; base: number } {
  if (data instanceof ArrayBuffer) return { buffer: data, base: 0 };
  const { buffer, byteOffset, byteLength } = data;
  if (byteOffset % 8 === 0 && buffer instanceof ArrayBuffer) {
    return { buffer, base: byteOffset };
  }
  // E.g. a pooled Node Buffer: typed views need 4-byte aligned offsets.
  const copy = new ArrayBuffer(byteLength);
  new Uint8Array(copy).set(new Uint8Array(buffer, byteOffset, byteLength));
  return { buffer: copy, base: 0 };
}

/** Parse the prefix and the JSON header (`data` may be only the start of a file). */
export function readHeader(data: ArrayBuffer | ArrayBufferView): ToolpathHeader {
  const bytes =
    data instanceof ArrayBuffer
      ? new Uint8Array(data)
      : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  if (bytes.byteLength < PREFIX_BYTES) throw new ToolpathFormatError('truncated toolpath file');
  const view = new DataView(bytes.buffer, bytes.byteOffset, PREFIX_BYTES);
  const magic = String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]);
  if (magic !== MAGIC) throw new ToolpathFormatError('not a luregen toolpath file');
  const version = view.getUint16(4, true);
  if (version !== VERSION) throw new ToolpathFormatError(`unsupported toolpath version ${version}`);
  const headerLen = view.getUint32(8, true);
  if (bytes.byteLength < PREFIX_BYTES + headerLen) {
    throw new ToolpathFormatError('truncated toolpath header');
  }
  const json = new TextDecoder().decode(bytes.subarray(PREFIX_BYTES, PREFIX_BYTES + headerLen));
  return JSON.parse(json) as ToolpathHeader;
}

/** Parse a whole file: header plus zero-copy typed-array views. */
export function readToolpath(data: ArrayBuffer | ArrayBufferView): Toolpath {
  const header = readHeader(data);
  const { buffer, base } = asAligned(data);
  const total = data.byteLength;
  const arrays: Record<string, Float32Array | Uint8Array | Uint16Array | Uint32Array> = {};
  for (const a of header.arrays) {
    const Ctor = CTORS[a.dtype];
    if (!Ctor) throw new ToolpathFormatError(`unknown dtype ${a.dtype} for ${a.name}`);
    const length = a.count * a.components;
    if (a.offset % Ctor.BYTES_PER_ELEMENT !== 0) {
      throw new ToolpathFormatError(`misaligned array ${a.name}`);
    }
    if (a.offset + length * Ctor.BYTES_PER_ELEMENT > total) {
      throw new ToolpathFormatError(`truncated array ${a.name}`);
    }
    arrays[a.name] = new Ctor(buffer, base + a.offset, length);
  }
  const n = header.segments;
  const need = (name: string, len: number) => {
    const arr = arrays[name];
    if (!arr) throw new ToolpathFormatError(`missing array ${name}`);
    if (arr.length !== len) throw new ToolpathFormatError(`array ${name} has the wrong length`);
    return arr;
  };
  const common = {
    header,
    segments: n,
    points: need('points', n === 0 ? 0 : 3 * (n + 1)) as Float32Array,
    kind: need('kind', n) as Uint8Array,
    tool: need('tool', n) as Uint8Array,
    feed: need('feed', n) as Float32Array,
    line: need('line', n) as Uint32Array,
  };
  if (header.kind === 'print') {
    return {
      type: 'print',
      ...common,
      feature: need('feature', n) as Uint8Array,
      layer: need('layer', n) as Uint16Array,
      width: need('width', n) as Uint16Array,
      height: need('height', n) as Uint16Array,
    };
  }
  if (header.kind === 'cnc') return { type: 'cnc', ...common };
  throw new ToolpathFormatError(`unknown toolpath kind ${String(header.kind)}`);
}

/** Fetch and parse a toolpath URL (the server gzips; `fetch` inflates transparently). */
export async function fetchToolpath(url: string, signal?: AbortSignal): Promise<Toolpath> {
  const res = await fetch(url, { signal });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (body.detail) detail = String(body.detail);
    } catch {
      // not JSON
    }
    throw new Error(detail);
  }
  return readToolpath(await res.arrayBuffer());
}

/** xyz of point `i` (segment i starts there, segment i-1 ends there). */
export function pointAt(tp: Toolpath, i: number): Vec3 {
  const p = tp.points;
  return [p[3 * i], p[3 * i + 1], p[3 * i + 2]];
}

/** Print: the layer index containing segment `s` (binary search over `layer_offsets`). */
export function layerOfSegment(offsets: readonly number[], s: number): number {
  let lo = 0;
  let hi = offsets.length - 2;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (offsets[mid] <= s) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}
