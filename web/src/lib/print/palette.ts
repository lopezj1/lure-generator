/**
 * Print preview colors. Our own palette: the families follow the slicer convention
 * (warm walls, cool supports, green solid fills), the values are chosen here.
 */

const FEATURES: Record<string, string> = {
  'Outer wall': '#f0a020',
  'Inner wall': '#e8d040',
  'Overhang wall': '#3060e0',
  'Sparse infill': '#b84a3c',
  'Internal solid infill': '#a040b0',
  'Solid infill': '#a040b0',
  'Top surface': '#e05050',
  'Bottom surface': '#60b060',
  Bridge: '#5aa0d8',
  'Internal Bridge': '#78b8e0',
  'Gap infill': '#ffffff',
  Ironing: '#ff9090',
  Skirt: '#30c0a0',
  Brim: '#30c0a0',
  'Skirt/Brim': '#30c0a0',
  Support: '#40c060',
  'Support interface': '#2878b8',
  'Support transition': '#5a9070',
  'Prime tower': '#9a8070',
  'Wipe tower': '#9a8070',
  Custom: '#6c7a88',
  Unknown: '#8a8a8a',
};

export const TRAVEL_COLOR = '#4a80c0';
export const PAUSE_COLOR = '#ff7a1a';
export const BACKGROUND = '#24272d';

/** Stable fallback for feature names we don't know (golden-angle hues). */
function fallback(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  const hue = (h * 137.508) % 360;
  return `hsl(${hue.toFixed(0)} 60% 58%)`;
}

export function featureColor(name: string): string {
  return FEATURES[name] ?? fallback(name);
}

/** `#RRGGBB` → [r, g, b] in 0…1 (sRGB, written to the framebuffer as is). */
export function rgb(hex: string): [number, number, number] {
  if (hex.startsWith('hsl')) return hslToRgb(hex);
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return [0.6, 0.6, 0.6];
  const v = parseInt(m[1], 16);
  return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255];
}

function hslToRgb(css: string): [number, number, number] {
  const [h, s, l] = (css.match(/[\d.]+/g) ?? ['0', '0', '50']).map(Number);
  const sat = s / 100;
  const lig = l / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sat * Math.min(lig, 1 - lig);
  const f = (n: number) => lig - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [f(0), f(8), f(4)];
}

/**
 * Filament color for display on the dark background: very dark filaments are lifted to a
 * visible charcoal so black PLA doesn't vanish.
 */
export function toolColor(hex: string | undefined): [number, number, number] {
  const c = rgb(hex ?? '#a0a0a0');
  const luma = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  if (luma >= 0.12) return c;
  const lift = 0.3 - luma;
  return [c[0] + lift, c[1] + lift, c[2] + lift];
}

export function cssColor(c: [number, number, number]): string {
  const h = (v: number) =>
    Math.round(Math.max(0, Math.min(1, v)) * 255)
      .toString(16)
      .padStart(2, '0');
  return `#${h(c[0])}${h(c[1])}${h(c[2])}`;
}
