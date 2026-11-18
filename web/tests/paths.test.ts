// Unit tests for the editor's pure helpers. Run: `node --test tests/*.test.ts` (from web/; Node 24
// strips the types itself, no extra dependencies).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  deepEqual,
  derivedFields,
  getAt,
  parseBuildDir,
  savePayload,
  setAt,
  OVERRIDE_RE,
} from '../src/lib/editor/paths.ts';

const schema = {
  properties: {
    buoyancy: { properties: { water_density: { 'x-derived': 'computed' } } },
    process: {
      properties: { material: {}, material_density: { 'x-derived': 'catalog' } },
    },
    hardware: {
      items: { properties: { id: {}, mass_g: { 'x-derived': 'catalog' } } },
    },
  },
};
const derived = derivedFields(schema);

const loaded = () => ({
  buoyancy: { water_density: 1.025 },
  process: { material: 'PETG', material_density: 1.27 },
  hardware: [
    { id: 'eye', mass_g: [0.25, 0.35] },
    { id: 'treble', mass_g: [1.1, 1.4] },
  ],
});

test('derivedFields finds catalog and computed fields', () => {
  assert.deepEqual(derived, [
    [['buoyancy', 'water_density'], 'computed'],
    [['process', 'material_density'], 'catalog'],
    [['hardware', '*', 'mass_g'], 'catalog'],
  ]);
});

test('get/set/deepEqual', () => {
  const d: Record<string, unknown> = {};
  setAt(d, ['a', 'b'], 1);
  assert.equal(getAt(d, ['a', 'b']), 1);
  setAt(d, ['a', 'b'], undefined);
  assert.deepEqual(d, { a: {} });
  assert.ok(deepEqual({ a: null }, {}));
  assert.ok(!deepEqual([1, 2], [1, 3]));
});

test('an untouched config drops catalog-filled and computed values', () => {
  const out = savePayload(loaded(), loaded(), {}, derived);
  assert.deepEqual(out, {
    buoyancy: {},
    process: { material: 'PETG' },
    hardware: [{ id: 'eye' }, { id: 'treble' }],
  });
});

test('a material change does not keep the stale density', () => {
  const v = loaded();
  v.process.material = 'PLA';
  assert.deepEqual(savePayload(v, loaded(), {}, derived).process, { material: 'PLA' });
});

test('user overrides and layer-set catalog fields are kept', () => {
  const v = loaded();
  v.process.material_density = 1.3;
  v.hardware[1].mass_g = [1.2, 1.3];
  const out = savePayload(v, loaded(), { '/hardware/0/mass_g': 0 }, derived);
  assert.equal((out.process as Record<string, unknown>).material_density, 1.3);
  assert.deepEqual(out.hardware, [
    { id: 'eye', mass_g: [0.25, 0.35] },
    { id: 'treble', mass_g: [1.2, 1.3] },
  ]);
});

test('reordering hardware does not bake catalog values; a changed id drops them', () => {
  const v = loaded();
  v.hardware.reverse();
  assert.deepEqual(savePayload(v, loaded(), {}, derived).hardware, [
    { id: 'treble' },
    { id: 'eye' },
  ]);
  const w = loaded();
  w.hardware[0].id = 'ring';
  assert.deepEqual(savePayload(w, loaded(), {}, derived).hardware, [
    { id: 'ring' },
    { id: 'treble' },
  ]);
});

test('parseBuildDir and override syntax', () => {
  assert.deepEqual(parseBuildDir('builds/glide/v1/20261118-120000-abc'), {
    path: 'builds/glide/v1/20261118-120000-abc',
    family: 'glide',
    variant: 'v1',
    stamp: '20261118-120000-abc',
  });
  assert.equal(parseBuildDir('/srv/data/builds/glide/v1/s')?.path, 'builds/glide/v1/s');
  assert.equal(parseBuildDir('elsewhere/x'), null);
  assert.ok(OVERRIDE_RE.test('buoyancy.target_sink=suspend'));
  assert.ok(!OVERRIDE_RE.test('1bad=x'));
});
