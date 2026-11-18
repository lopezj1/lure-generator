// The basic theme plus a dispatcher on the server's `x-widget` hints (spike #57): widget
// choice lives in the form schema, so no per-path ui-schema is needed.
import { theme as basic } from '@sjsf/basic-theme';
import type { Config } from '@sjsf/form';
import Label from './widgets/Label.svelte';
import MaterialSelect from './widgets/MaterialSelect.svelte';
import PathPicker from './widgets/PathPicker.svelte';
import RangePairField from './widgets/RangePairField.svelte';
import SliderNumber from './widgets/SliderNumber.svelte';
import Title from './widgets/Title.svelte';

const byHint: Record<string, Record<string, unknown>> = {
  slider: { numberWidget: SliderNumber },
  'range-pair': { tupleField: RangePairField },
  'material-select': { textWidget: MaterialSelect },
  path: { textWidget: PathPicker },
};

type Basic = typeof basic;

export const theme = ((type: string, config: Config) => {
  const hint = (config?.schema as Record<string, unknown> | undefined)?.['x-widget'];
  const hit = typeof hint === 'string' ? byHint[hint]?.[type] : undefined;
  if (hit) return hit;
  if (type === 'label') return Label;
  if (type === 'title') return Title;
  return (basic as (t: string, c: Config) => unknown)(type, config);
}) as unknown as Basic;
