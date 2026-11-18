# Viewer toolpath binary format (v1)

The print and CNC previews (W6, W7) get G-code as one compact binary blob instead of
parsing text in the browser (epic #54, decision 1). The Python encoder lives in
`luregen.viewer` (`toolpath.py` extracts the segments, `binfmt.py` writes the bytes), and
`lure toolpath <file>` runs it from the terminal. The API serves the same bytes, compressed,
at `GET /builds/{path}/toolpath/{file}.bin` (W1).

## Model

A file is **one polyline of N+1 points**. Segment `i` runs from `points[i]` to
`points[i+1]` and has a kind, a tool, a feed and a source line; print files add a feature,
a layer, a width and a height. Every move starts where the previous one ended, so storing
points once (not start/end pairs) halves the position data. Where the path is discontinuous
(homing, a `G92 X…` reset, the first known position), the encoder inserts a **jump**
segment, kind 0, which viewers must not draw.

## Layout

All numbers are little-endian.

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic `LGTP` |
| 4 | 2 | `u16` version = 1 |
| 6 | 2 | `u16` flags = 0 (reserved) |
| 8 | 4 | `u32` `header_len` |
| 12 | `header_len` | UTF-8 JSON header, padded with spaces so `12 + header_len` is a multiple of 8 |
| 12 + `header_len` | … | typed arrays, each starting on an 8-byte boundary |

`header.arrays` lists every array as `{name, dtype, offset, count, components}`. The
`offset` is from the **start of the file**, so a reader creates views directly:

```ts
const view = new DataView(buf);
const headerLen = view.getUint32(8, true);
const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 12, headerLen)));
const arr = Object.fromEntries(header.arrays.map((a) => {
  const T = { float32: Float32Array, uint8: Uint8Array, uint16: Uint16Array, uint32: Uint32Array }[a.dtype];
  return [a.name, new T(buf, a.offset, a.count * a.components)];
}));
// arr.points: xyz triplets; segment i = arr.points[3*i .. 3*i+5]
```

Check `magic` and `version` before reading; refuse an unknown version.

| Array | dtype | Count | Meaning |
|---|---|---|---|
| `points` | float32 ×3 | N+1 | xyz in mm (machine/work coordinates as in the file) |
| `kind` | uint8 | N | index into `header.kinds`; 0 = `jump` (never drawn) |
| `tool` | uint8 | N | index into `header.tools` |
| `feed` | float32 | N | mm/min; 0 for jumps and CNC rapids, 0 if unknown |
| `line` | uint32 | N | 1-based source line (sync with a G-code listing) |
| `feature` | uint8 | N | print only: index into `header.features` (`;TYPE:`) |
| `layer` | uint16 | N | print only: index into `header.layers` |
| `width` | uint16 | N | print only: extrusion width in µm (`;WIDTH:`), 0 when not extruding |
| `height` | uint16 | N | print only: layer height in µm (`;HEIGHT:`) |
| `time` | float32 ×2 | N | CNC only: job time in s at the segment's start and end (see below) |

Arrays appear in that order; print-only arrays are absent from CNC files, and `time` is absent
from print files. Readers look arrays up by name and ignore names they don't know, so new
optional arrays don't need a version bump (see `params.encoder`).

## Header

Common keys:

- `format: "luregen-toolpath"`, `version: 1`, `kind: "print" | "cnc"`, `segments: N`.
- `kinds`: print `["jump", "travel", "extrude"]`; CNC `["jump", "rapid", "feed", "plunge"]`.
  A plunge is a straight feed move straight down (−Z only). Arcs are feed moves.
- `tools`: list; `tool[i]` indexes it.
  - Print: one entry per extruder, index = T number, with `number`, `color`
    (`filament_colour`), `filament_type`, `filament` (settings id) and `filament_mm`
    (net E per tool).
  - CNC: tools in order of first use, with `number` (T word; `null` if the file never
    selects one), plus `diameter`, `corner_radius`, `zmin` and `description` when the file
    has Fusion `(T1 D=3.175 CR=0 - ZMIN=… - flat end mill)` or Carbide `(TOOL/MILL,d,r,…)`
    comments, and `spindle_rpm` (the first `M3 S…` while the tool is loaded).
- `markers`: in file order. Each has `type`, `line` and `segment` (the index of the first
  segment *after* the event; draw a marker at `points[segment]`).
  - `tool_change`: `from`, `tool` (tool indexes); print markers also have `layer`. The
    first tool selection of a file is not a change.
  - `pause`: `command` (print: `M600`, `PAUSE`, `M601` and the file's `machine_pause_gcode`;
    CNC: `M0`/`M1`); print markers also have `layer`.
  - CNC markers also have `time_s`: the job time when the event happens.
- `bounds`: `{min: [x,y,z], max: [x,y,z]}` over all points (null when empty).
  `bounds_active`: the same over extruding (print) or cutting (CNC: feed and plunge)
  segments only; use it to frame the camera.
- `stats`:
  - `kinds`: `{name: {segments, length_mm}}`.
  - `tools`: `[{tool, number, segments, active_length_mm, rapid_length_mm, time_s}]`.
    Active means extrude or feed+plunge; rapid means travel or rapid.
  - `time_s`: print: Σ length / feed. It ignores acceleration, so print times run well
    under the slicer's estimate. CNC: the end of the job timeline (motion plus waits).
  - CNC only: `motion_s`, `wait_s` (tool changes and dwells), `tool_changes` (`M6` count,
    including the first), and per tool `cut_time_s` and `rapid_time_s` (`time_s` is their
    sum, motion only).
  - Print only: `features: {name: {segments, length_mm}}` (extruding segments).
- `source`: print has `name`, `generator`, `header_layers`, `tool_changes` (Orca's
  `total filament change`), `estimated_time` and `estimated_time_s`; CNC has `name`,
  `skipped_lines` and `skipped_count` (lines the tokenizer rejected).
- `params`: encoder settings (`tolerance`, `max_arc_segments`, plus `pause_commands`
  (print) or `rapid_rates` `[x, y, z]` mm/min and `tool_change_s` (CNC)), and `encoder`:
  the encoder revision within format v1 (2 added the CNC `time` array).

Print only:

- `features`: names in first-seen order; index 0 is `Unknown` (moves before any `;TYPE:`).
- `layers`: `[{z, height, start, line}]`. Entry 0 holds the start G-code (moves before the
  first `;LAYER_CHANGE`, e.g. the purge line); entries 1…`layer_count` are the slicer's
  layers. `z` comes from `;Z:` and `height` from the layer's first `;HEIGHT:`.
- `layer_offsets`: `len(layers) + 1` prefix offsets; layer i is segments
  `[layer_offsets[i], layer_offsets[i+1])`. Segments are in file order, which is layer
  order, so "show layers ≤ k" is a single draw range `0 … layer_offsets[k+1]`.
- `layer_count`: number of `;LAYER_CHANGE` blocks; equals Orca's `total layer number`.

CNC only:

- `clearance_z`: machine-coordinate moves (`G53 G0 Z-5`, `G28 G91 Z0`) end at an unknown
  work Z. They are drawn at `clearance_z` = the highest known Z + 5 mm, keeping X/Y when the
  move doesn't name them. A `G28 X0 Y0` home is not drawn.
- `stock`: `{min, max}` from Carbide-style `(stockMin:…)` / `(stockMax:…)` comments, when
  present.

## CNC job timeline

The `time` array gives each segment's start and end in seconds from the start of the file,
for job-progress scrubbing. A feed or plunge takes length / F; a rapid takes its slowest axis
at `params.rapid_rates` (default 5000/5000/2500 mm/min, the Shapeoko's `rapid_mm_min`); a
jump takes no time. Waits sit *between* segments: each `M6` adds `params.tool_change_s`
(default 60 s: prompt, swap, BitSetter probe) and each `G4 P…` dwell its seconds, so the next
segment starts later than the previous one ended. Acceleration is ignored, as in
`fab.router.verify`'s runtime estimate.

## Arcs

`G2`/`G3` in G17, G18 and G19 (and Orca's helical spiral-lift `G3 Z… I… J… P1`) are
tessellated in their own plane. Chords deviate from the true circle by at most
`params.tolerance` (default 0.02 mm), with at most `params.max_arc_segments` (512) segments
per arc. The normal axis moves linearly (helix), and the polyline ends exactly on the
programmed end point. All segments of one arc share its `line`.

## Caching

`encode_file(path, kind="auto")` writes `<file>.<kind>.bin` next to the source and reuses it
when it's newer than the source and was written with the same format version, encoder
revision and parameters. Writes are atomic (temp file + rename). `kind="auto"` looks at the first 64 kB:
slicer comments (`; generated by`, `;LAYER_CHANGE`, `;TYPE:`, `;FLAVOR:`) or E words mean
print; anything else is CNC.

## Sizes (owner samples, 2-core home server)

| File | Lines | Segments | Encode | .bin | gzip |
|---|---|---|---|---|---|
| U1 multi-material print (4 tools, 498 layers, 157 changes) | 617k | 455k | 2.3 s | 13.2 MB | 3.7 MB |
| Fusion 3D finishing, 1/32" flat, G18 arcs | 64k | 68k | 1.2 s | 1.5 MB | 0.5 MB |
| Fusion 3D roughing + contour, G18/G19 arcs | 23k | 29k | 0.5 s | 0.6 MB | 0.25 MB |
