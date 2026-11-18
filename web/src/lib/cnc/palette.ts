/**
 * CNC preview colors: a categorical tool palette chosen for the dark viewport (our own
 * values), plus the stock, bounding box and rapid tints.
 */

const TOOLS = [
  '#4fc3f7', // sky
  '#ffb74d', // amber
  '#81c784', // green
  '#f06292', // pink
  '#ba68c8', // violet
  '#fff176', // yellow
  '#4db6ac', // teal
  '#ff8a65', // coral
  '#9fa8da', // periwinkle
  '#dce775', // lime
];

export const STOCK_COLOR = '#c8a46e';
export const BBOX_COLOR = '#7fb0ff';
export const MARKER_COLOR = '#ff7a1a';

export function cncToolColor(index: number): string {
  return TOOLS[((index % TOOLS.length) + TOOLS.length) % TOOLS.length];
}
