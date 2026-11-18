// Theme = basic theme + a dispatcher on the server's `x-widget` hints. No per-path ui-schema needed.
import { theme as basic } from "@sjsf/basic-theme";
import SliderNumber from "./widgets/SliderNumber.svelte";
import RangePairField from "./widgets/RangePairField.svelte";
import MaterialSelect from "./widgets/MaterialSelect.svelte";
import Label from "./widgets/Label.svelte";
import Title from "./widgets/Title.svelte";

const byHint: Record<string, Record<string, unknown>> = {
  slider: { numberWidget: SliderNumber },
  "range-pair": { tupleField: RangePairField },
  "material-select": { textWidget: MaterialSelect },
};

export const theme = ((type: string, config: any) => {
  const hint = config?.schema?.["x-widget"];
  const hit = hint && byHint[hint]?.[type];
  if (hit) return hit;
  if (type === "label") return Label;
  if (type === "title") return Title;
  return (basic as any)(type, config);
}) as typeof basic;
