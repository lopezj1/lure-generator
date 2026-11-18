import type { components } from '../api';

export type BuildSummary = components['schemas']['BuildSummary'];
export type ModelInfo = components['schemas']['ModelInfo'];
export type Section = components['schemas']['Section'];
export type SectionLoop = components['schemas']['SectionLoop'];
export type Axis = Section['axis'];

export const AXES: Axis[] = ['x', 'y', 'z'];

/** URL prefix of a build's API routes. */
export function buildBase(b: Pick<BuildSummary, 'family' | 'variant' | 'stamp'>): string {
  return `/api/builds/${b.family}/${b.variant}/${b.stamp}`;
}

/** A build with a model to view: at least one `model/*.step`. */
export function hasModel(b: BuildSummary): boolean {
  return b.files.some((f) => /^model\/[^/]+\.(step|stp)$/i.test(f.name));
}
