/**
 * Checks the TS toolpath reader against the Python encoder (no npm test runner needed).
 *
 * `tests/unit/test_toolpath_reader_ts.py` encodes fixtures, writes the expected values as
 * JSON, and runs `node --test web/tests/toolpath-reader.test.ts` with
 * LUREGEN_READER_CASES=<cases.json> (Node >= 22.18 strips the types itself).
 * Optional: LUREGEN_READER_PERF=<file.bin> times parsing plus the print buffers on a big file.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { buildPrintBuffers, drawRange, layerTicks } from '../src/lib/print/model.ts';
import {
  ToolpathFormatError,
  layerOfSegment,
  pointAt,
  readHeader,
  readToolpath,
} from '../src/lib/toolpath/reader.ts';

interface Case {
  bin: string;
  kind: 'print' | 'cnc';
  segments: number;
  arrays: Record<string, { length: number; sum: number; first: number; last: number }>;
  first_point: number[];
  last_point: number[];
  layer_offsets?: number[];
  markers: number;
  pauses: number;
}

const casesFile = process.env.LUREGEN_READER_CASES;

test('reader matches the Python encoder', { skip: !casesFile }, () => {
  const cases = JSON.parse(readFileSync(casesFile!, 'utf8')) as Case[];
  assert.ok(cases.length > 0);
  for (const c of cases) {
    const file = readFileSync(c.bin); // a Node Buffer: possibly pooled / misaligned
    const tp = readToolpath(file);
    assert.equal(tp.type, c.kind);
    assert.equal(tp.segments, c.segments);
    assert.equal(tp.header.markers.length, c.markers);
    for (const [name, exp] of Object.entries(c.arrays)) {
      const arr = (tp as unknown as Record<string, ArrayLike<number>>)[name];
      assert.ok(arr, `${c.bin}: ${name} missing`);
      assert.equal(arr.length, exp.length, `${name} length`);
      let sum = 0;
      for (let i = 0; i < arr.length; i++) sum += arr[i];
      assert.ok(Math.abs(sum - exp.sum) <= 1e-6 * Math.max(1, Math.abs(exp.sum)), `${name} sum`);
      if (arr.length) {
        assert.ok(Math.abs(arr[0] - exp.first) < 1e-4, `${name}[0]`);
        assert.ok(Math.abs(arr[arr.length - 1] - exp.last) < 1e-4, `${name}[-1]`);
      }
    }
    if (c.segments) {
      pointAt(tp, 0).forEach((v, i) => assert.ok(Math.abs(v - c.first_point[i]) < 1e-4));
      pointAt(tp, c.segments).forEach((v, i) => assert.ok(Math.abs(v - c.last_point[i]) < 1e-4));
    }
    // Zero-copy: an aligned ArrayBuffer yields views on that same buffer.
    const ab = file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength);
    const views = readToolpath(ab);
    assert.equal(views.points.buffer, ab);
    assert.deepEqual(readHeader(ab.slice(0, 12 + new DataView(ab).getUint32(8, true))), tp.header);

    if (tp.type === 'print') {
      const offsets = tp.header.layer_offsets!;
      assert.deepEqual(offsets, c.layer_offsets);
      for (let l = 0; l < offsets.length - 1; l++) {
        if (offsets[l + 1] > offsets[l]) {
          assert.equal(layerOfSegment(offsets, offsets[l]), l);
          assert.equal(layerOfSegment(offsets, offsets[l + 1] - 1), l);
        }
        for (let s = offsets[l]; s < offsets[l + 1]; s++) assert.equal(tp.layer[s], l);
      }
      const { index, attrs } = buildPrintBuffers(tp);
      assert.equal(index.length, 2 * tp.segments);
      assert.equal(attrs.length, 4 * (tp.segments + 1));
      for (let s = 0; s < tp.segments; s += 97) {
        assert.equal(index[2 * s], s + 1);
        assert.equal(index[2 * s + 1], s);
        assert.equal(attrs[4 * s], tp.kind[s]);
        assert.equal(attrs[4 * s + 1], tp.feature[s]);
        assert.equal(attrs[4 * s + 2], tp.tool[s]);
      }
      const top = offsets.length - 2;
      assert.deepEqual(drawRange(offsets, 0, top, Infinity), { start: 0, end: tp.segments });
      assert.deepEqual(drawRange(offsets, 1, 1, 3), {
        start: offsets[1],
        end: Math.min(offsets[1] + 3, offsets[2]),
      });
      const ticks = layerTicks(tp);
      assert.equal(ticks.filter((t) => t.type === 'pause').length, c.pauses);
    }
  }
});

test('reader rejects bad files', () => {
  const bad = new Uint8Array(16);
  assert.throws(() => readToolpath(bad), ToolpathFormatError);
  assert.throws(() => readToolpath(new Uint8Array(4)), /truncated/);
  const v2 = new Uint8Array(16);
  v2.set([0x4c, 0x47, 0x54, 0x50, 2, 0]);
  assert.throws(() => readToolpath(v2), /unsupported toolpath version 2/);
});

const perfFile = process.env.LUREGEN_READER_PERF;

test('perf: parse and build print buffers', { skip: !perfFile }, () => {
  const file = readFileSync(perfFile!);
  const ab = file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength);
  const t0 = performance.now();
  const tp = readToolpath(ab);
  const t1 = performance.now();
  assert.equal(tp.type, 'print');
  if (tp.type !== 'print') return;
  buildPrintBuffers(tp);
  const t2 = performance.now();
  console.log(
    `${tp.segments} segments: parse ${(t1 - t0).toFixed(1)} ms, buffers ${(t2 - t1).toFixed(1)} ms`,
  );
});
