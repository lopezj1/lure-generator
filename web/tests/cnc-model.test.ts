/**
 * CNC preview model (W7, #68) against Python-encoded fixtures.
 *
 * `tests/unit/test_toolpath_reader_ts.py::test_ts_cnc_model` encodes the golden NC files,
 * writes probes (job time → expected tool position) as JSON and runs
 * `node --test web/tests/cnc-model.test.ts` with LUREGEN_CNC_CASES=<cases.json>.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  buildJob,
  buildPartBuffers,
  formatClock,
  playheadAt,
  segmentAt,
  stepTime,
} from '../src/lib/cnc/model.ts';
import { readToolpath, type CncToolpath } from '../src/lib/toolpath/reader.ts';

interface Probe {
  t: number;
  pos: [number, number, number];
  part: number;
  waiting: boolean;
  tool_number: number | null;
}

interface Case {
  files: { label: string; bin: string }[];
  duration: number;
  tool_numbers: (number | null)[];
  tool_changes: number;
  markers: number;
  probes: Probe[];
}

const casesFile = process.env.LUREGEN_CNC_CASES;

function load(c: Case) {
  return buildJob(
    c.files.map((f) => {
      const tp = readToolpath(readFileSync(f.bin));
      assert.equal(tp.type, 'cnc');
      return { label: f.label, tp: tp as CncToolpath };
    }),
  );
}

test('job timeline and playhead match the Python encoder', { skip: !casesFile }, () => {
  const cases = JSON.parse(readFileSync(casesFile!, 'utf8')) as Case[];
  assert.ok(cases.length > 0);
  for (const c of cases) {
    const job = load(c);
    assert.ok(Math.abs(job.duration - c.duration) < 0.05, `duration ${job.duration}`);
    assert.deepEqual(
      job.tools.map((t) => t.number),
      c.tool_numbers,
    );
    assert.equal(job.toolChanges, c.tool_changes);
    assert.equal(job.markers.length, c.markers);
    for (const m of job.markers) assert.ok(m.time >= 0 && m.time <= job.duration);
    for (const p of c.probes) {
      const h = playheadAt(job, p.t)!;
      assert.equal(h.part, p.part, `part at ${p.t}`);
      assert.equal(h.waiting, p.waiting, `waiting at ${p.t}`);
      assert.equal(job.tools[h.tool].number, p.tool_number, `tool at ${p.t}`);
      h.pos.forEach((v, k) => assert.ok(Math.abs(v - p.pos[k]) < 1e-3, `pos at ${p.t}: ${h.pos}`));
    }

    for (const part of job.parts) {
      const n = part.tp.segments;
      const { index, attrs, dist, seg } = buildPartBuffers(part);
      let drawn = 0;
      for (let s = 0; s < n; s++) if (part.tp.kind[s] !== 0) drawn++;
      assert.equal(index.length, 2 * drawn);
      assert.equal(attrs.length, 4 * (n + 1));
      assert.equal(dist.length, n + 1);
      for (let s = 1; s <= n; s++) assert.ok(dist[s] >= dist[s - 1]);
      for (let s = 0; s < n; s++) {
        assert.ok(seg[4 * s] <= seg[4 * s + 1] + 1e-3);
        assert.ok(Math.abs(seg[4 * s] - (part.offset + part.times[2 * s])) < 1e-2);
      }
      if (n) {
        assert.equal(segmentAt(part.times, n, -1), 0);
        assert.equal(segmentAt(part.times, n, 1e12), n);
      }
    }

    // Stepping forward through every move ends exactly at the job's end, and back at 0.
    let t = 0;
    let steps = 0;
    while (t < job.duration && steps < 1e6) {
      const next = stepTime(job, t, 1);
      assert.ok(next > t, `step from ${t}`);
      t = next;
      steps++;
    }
    assert.ok(Math.abs(t - job.duration) < 0.05);
    const back = stepTime(job, stepTime(job, 0, 3), -3);
    assert.ok(back >= 0 && back < stepTime(job, 0, 1));
  }
});

test('formatClock', () => {
  assert.equal(formatClock(0), '0:00');
  assert.equal(formatClock(75.4), '1:15');
  assert.equal(formatClock(3725), '1:02:05');
});
