// App-wide "which build is current" store. Written by the config editor's Build action (W4)
// when a build job succeeds; read by the model viewer (W5), print/CNC previews and the
// builds/jobs panel (W9), which may also set it (e.g. when the user picks an older build).
//
//   import { currentBuild } from './lib/stores/build';
//   $currentBuild?.path   // "builds/glide/striper_175_petg/20261118-120000-abc1234"
//   fetch(buildFileUrl($currentBuild, 'model/body.stl'))
//
// Keep it minimal: anything more (lists, history) belongs to the component that needs it.
import { writable } from 'svelte/store';
import { parseBuildDir } from '../editor/paths';

export interface CurrentBuild {
  /** Build folder relative to the data root: `builds/<family>/<variant>/<stamp>`. */
  path: string;
  family: string;
  variant: string;
  /** Folder name (`<YYYYMMDD-HHMMSS>-<gitsha>[-dirty]`); `latest` also works with the API. */
  stamp: string;
  /** The job that produced it, when known. */
  jobId?: string;
}

export const currentBuild = writable<CurrentBuild | null>(null);

/** Make the build at `buildDir` (a job's `result.build_dir`) current. */
export function setCurrentBuild(buildDir: string, jobId?: string): CurrentBuild | null {
  const parsed = parseBuildDir(buildDir);
  if (parsed) currentBuild.set({ ...parsed, jobId });
  return parsed ? { ...parsed, jobId } : null;
}

/** URL of a file in a build: `GET /api/builds/{family}/{variant}/{stamp}/files/{name}`. */
export function buildFileUrl(build: CurrentBuild, name: string, download = false): string {
  const seg = [build.family, build.variant, build.stamp].map(encodeURIComponent).join('/');
  const file = name.split('/').map(encodeURIComponent).join('/');
  return `/api/builds/${seg}/files/${file}${download ? '?download=true' : ''}`;
}
