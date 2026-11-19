/**
 * CNC preview sources (W7, #68; follows the current build, #97): which NC files the picker
 * offers, and which one to load when the shared `currentBuild` changes. No runtime imports,
 * so it runs in Node for tests.
 */

export interface FileRef {
  label: string;
  url: string;
}

export interface Source {
  id: string;
  label: string;
  files: FileRef[];
  /** `family/variant/stamp` of the build the files come from (unset for uploads). */
  build?: string;
}

/** The parts of a `GET /api/builds` entry used here. */
export interface BuildListing {
  family: string;
  variant: string;
  stamp: string;
  path: string;
  latest: boolean;
  files: { name: string }[];
}

/** The parts of the `currentBuild` store used here (`stamp` may be `latest`). */
export interface BuildRef {
  family: string;
  variant: string;
  stamp: string;
}

const CNC_FILE = /^fab\/cnc\/(.+\.(?:nc|ngc|tap|cnc|gcode))$/i;

function byName(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}

export function buildKey(b: BuildRef): string {
  return `${b.family}/${b.variant}/${b.stamp}`;
}

/**
 * Picker entries for every build's `fab/cnc/*`: per build, "all files" as one job first (when
 * there is more than one), then each file on its own.
 */
export function cncSources(builds: readonly BuildListing[]): Source[] {
  const found: Source[] = [];
  for (const b of builds) {
    const parts = [b.family, b.variant, b.stamp].map(encodeURIComponent).join('/');
    const files: FileRef[] = [];
    for (const f of b.files) {
      const m = CNC_FILE.exec(f.name);
      if (!m) continue;
      const name = f.name.split('/').map(encodeURIComponent).join('/');
      files.push({ label: m[1], url: `/api/builds/${parts}/toolpath/${name}?kind=cnc` });
    }
    files.sort((a, z) => byName(a.label, z.label));
    const base = `${b.family}/${b.variant} ${b.stamp}`;
    const build = buildKey(b);
    if (files.length > 1) {
      found.push({
        id: `build:${b.path}:*`,
        label: `${base} · all ${files.length} files`,
        files,
        build,
      });
    }
    for (const f of files) {
      found.push({
        id: `build:${b.path}:${f.label}`,
        label: `${base} · ${f.label}`,
        files: [f],
        build,
      });
    }
  }
  return found;
}

/**
 * The source to load for build `cb`: all its NC files as one job (or its only file); null
 * when it has none. A `latest` stamp resolves to the build the listing flags as latest.
 */
export function sourceForBuild(
  sources: readonly Source[],
  builds: readonly BuildListing[],
  cb: BuildRef,
): Source | null {
  let key = buildKey(cb);
  if (cb.stamp === 'latest') {
    const b = builds.find((x) => x.latest && x.family === cb.family && x.variant === cb.variant);
    if (b) key = buildKey(b);
  }
  return sources.find((s) => s.build === key) ?? null;
}
