// Unit tests for the grouped editor's schema helpers (#121). Run: `node --test tests/*.test.ts`
// (from web/; Node 24 strips the types itself).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  children,
  countFields,
  errorCounts,
  errorsUnder,
  isContainer,
  newRow,
  nodeAt,
  parseLoose,
  sharedKeys,
  shows,
  type Node,
} from '../src/lib/editor/params/schema.ts';

const leaf = (group: string, level: string, extra: Node = {}): Node => ({
  type: 'number',
  'x-group': group,
  'x-level': level,
  ...extra,
});

const schema: Node = {
  type: 'object',
  'x-groups': [
    { id: 'shape', title: 'Shape' },
    { id: 'print', title: 'Print' },
    { id: 'cnc', title: 'CNC' },
  ],
  properties: {
    size: {
      type: 'object',
      'x-group': 'shape',
      'x-level': 'basic',
      properties: {
        length_mm: leaf('shape', 'basic'),
        solved_length_mm: leaf('shape', 'advanced'),
      },
    },
    dfm: {
      type: 'object',
      'x-group': 'advanced',
      'x-level': 'advanced',
      properties: {
        print: {
          type: 'object',
          'x-group': 'print',
          'x-level': 'advanced',
          properties: { min_wall_mm: leaf('print', 'advanced') },
        },
        cnc: {
          type: 'object',
          'x-group': 'cnc',
          'x-level': 'advanced',
          properties: { tools: { type: 'array', 'x-group': 'cnc', 'x-level': 'advanced' } },
        },
      },
    },
    hardware: {
      type: 'array',
      'x-layout': 'table',
      'x-group': 'hardware',
      'x-level': 'basic',
      items: {
        type: 'object',
        required: ['id', 'section'],
        properties: {
          id: { type: 'string' },
          section: { type: 'string', enum: ['head', 'tail'] },
          qty: { type: 'integer', default: 1 },
          mass_g: { type: 'array' },
        },
      },
    },
  },
};

test('shows() and children() select a tab and level', () => {
  const size = schema.properties!.size;
  assert.ok(shows(size, 'shape', 'basic'));
  assert.ok(shows(size, 'shape', 'advanced'));
  assert.ok(!shows(size, 'print'));
  // dfm spans two tabs through its children
  assert.ok(shows(schema.properties!.dfm, 'print'));
  assert.ok(shows(schema.properties!.dfm, 'cnc'));
  assert.deepEqual(
    children(size, 'shape', 'basic').map(([k]) => k),
    ['length_mm'],
  );
  assert.equal(countFields(size, 'shape', 'advanced'), 1);
  // a table is one control, not a container
  assert.ok(!isContainer(schema.properties!.hardware));
  assert.ok(shows(schema.properties!.hardware, 'hardware', 'basic'));
});

test('nodeAt() steps through array items', () => {
  assert.equal(nodeAt(schema, ['hardware', 3, 'qty'])?.type, 'integer');
  assert.equal(nodeAt(schema, ['size', 'nope']), undefined);
});

test('errorCounts() groups errors by the field they point at', () => {
  const counts = errorCounts(schema, [
    { path: ['size', 'length_mm'], message: 'too short' },
    { path: ['dfm', 'print', 'min_wall_mm'], message: 'x' },
    { path: [], message: 'general' },
  ]);
  assert.deepEqual(counts, { shape: 1, print: 1, '': 1 });
  assert.equal(errorsUnder([{ path: ['size', 'length_mm'], message: 'x' }], ['size']).length, 1);
  assert.equal(errorsUnder([{ path: ['sizes'], message: 'x' }], ['size']).length, 0);
});

test('newRow() fills defaults and required enums', () => {
  const item = (schema.properties!.hardware.items as Node)!;
  assert.deepEqual(newRow(item), { section: 'head', qty: 1 });
});

test('sharedKeys() and parseLoose()', () => {
  const a: Node = { properties: { tool: {}, mode: {}, enabled: {} } };
  const b: Node = { properties: { tool: {}, mode: {} } };
  assert.deepEqual(sharedKeys([a, b]), ['tool', 'mode']);
  assert.equal(parseLoose('3'), 3);
  assert.equal(parseLoose('false'), false);
  assert.equal(parseLoose('abc'), 'abc');
  assert.equal(parseLoose(''), '');
});
