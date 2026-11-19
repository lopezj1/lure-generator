// CNC tab sources and "follow the current build" (#97). Run: `node --test tests/*.test.ts`.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cncSources, sourceForBuild, type BuildListing } from '../src/lib/cnc/sources.ts';

const builds: BuildListing[] = [
  {
    family: 'glide',
    variant: 'a',
    stamp: '20261119-100000-abc1234',
    path: 'builds/glide/a/20261119-100000-abc1234',
    latest: true,
    files: [
      { name: 'report.md' },
      { name: 'fab/cnc/op10_T2.nc' },
      { name: 'fab/cnc/op2_T1.nc' },
      { name: 'fab/print/head.gcode' },
    ],
  },
  {
    family: 'glide',
    variant: 'a',
    stamp: '20261118-100000-abc1234',
    path: 'builds/glide/a/20261118-100000-abc1234',
    latest: false,
    files: [{ name: 'fab/cnc/only.nc' }],
  },
  {
    family: 'glide',
    variant: 'b',
    stamp: '20261118-090000-abc1234',
    path: 'builds/glide/b/20261118-090000-abc1234',
    latest: true,
    files: [{ name: 'model/body.stl' }],
  },
];

test('lists all files as one job first, then each file in natural order', () => {
  const s = cncSources(builds);
  assert.deepEqual(
    s.map((x) => x.label),
    [
      'glide/a 20261119-100000-abc1234 · all 2 files',
      'glide/a 20261119-100000-abc1234 · op2_T1.nc',
      'glide/a 20261119-100000-abc1234 · op10_T2.nc',
      'glide/a 20261118-100000-abc1234 · only.nc',
    ],
  );
  assert.equal(
    s[0].files[0].url,
    '/api/builds/glide/a/20261119-100000-abc1234/toolpath/fab/cnc/op2_T1.nc?kind=cnc',
  );
});

test('the current build loads all of its NC files; switching swaps them', () => {
  const s = cncSources(builds);
  const cur = sourceForBuild(s, builds, {
    family: 'glide',
    variant: 'a',
    stamp: '20261119-100000-abc1234',
  });
  assert.equal(cur?.files.length, 2);
  assert.match(cur!.id, /:\*$/);
  const old = sourceForBuild(s, builds, {
    family: 'glide',
    variant: 'a',
    stamp: '20261118-100000-abc1234',
  });
  assert.equal(old?.label, 'glide/a 20261118-100000-abc1234 · only.nc');
  const latest = sourceForBuild(s, builds, { family: 'glide', variant: 'a', stamp: 'latest' });
  assert.equal(latest?.id, cur?.id);
  assert.equal(sourceForBuild(s, builds, { family: 'glide', variant: 'b', stamp: 'latest' }), null);
});
