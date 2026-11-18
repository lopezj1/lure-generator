// Unit tests for the builds/jobs drawer's display helpers. Run: `node --test tests/*.test.ts`.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  bytes,
  cliCommand,
  elapsed,
  jobTarget,
  num,
  signed,
  stampSha,
  stampTime,
  togglePick,
} from '../src/lib/drawer/format.ts';

test('numbers and signed deltas', () => {
  assert.equal(num(1.234), '1.23');
  assert.equal(num(null), '–');
  assert.equal(num(2, 0), '2');
  assert.equal(signed(0.5), '+0.50');
  assert.equal(signed(-1.25), '−1.25');
  assert.equal(signed(-0.001), '±0.00');
  assert.equal(signed(undefined), '–');
});

test('bytes', () => {
  assert.equal(bytes(812), '812 B');
  assert.equal(bytes(14_200), '14.2 kB');
  assert.equal(bytes(3_100_000), '3.1 MB');
  assert.equal(bytes(null), '');
});

test('stamps', () => {
  assert.equal(stampTime('20261118-140312-abc1234-dirty'), '2026-11-18 14:03');
  assert.equal(stampSha('20261118-140312-abc1234-dirty'), 'abc1234-dirty');
  assert.equal(stampTime('weird'), 'weird');
  assert.equal(stampSha('weird'), '');
});

test('cli command from job argv', () => {
  const argv = ['/usr/bin/python3', '-m', 'luregen', '--progress', 'json', 'build', 'v.toml'];
  assert.equal(cliCommand([...argv, '--set', 'a.b=1']), 'lure build v.toml --set a.b=1');
  assert.equal(cliCommand([...argv, '--set', 'name=a b']), `lure build v.toml --set 'name=a b'`);
});

test('job target', () => {
  const build = { op: 'build', args: { variant: 'spec/designs/glide/variants/a.toml' } };
  assert.equal(jobTarget(build), 'glide/variants/a.toml');
  assert.equal(
    jobTarget({ op: 'fab_cnc', args: { build: 'builds/glide/a/latest' } }),
    'glide/a/latest',
  );
  assert.equal(jobTarget({ op: 'profile', args: { image: 'uploads/x.png' } }), 'uploads/x.png');
});

test('elapsed', () => {
  assert.equal(elapsed('2026-01-01T00:00:00Z', '2026-01-01T00:00:12Z'), '12 s');
  assert.equal(elapsed('2026-01-01T00:00:00Z', '2026-01-01T00:03:05Z'), '3 min 5 s');
  assert.equal(elapsed(null), '');
});

test('compare picks keep at most two, oldest dropped', () => {
  let p: string[] = [];
  p = togglePick(p, 'a');
  p = togglePick(p, 'b');
  assert.deepEqual(p, ['a', 'b']);
  assert.deepEqual(togglePick(p, 'c'), ['b', 'c']);
  assert.deepEqual(togglePick(p, 'a'), ['b']);
});
