// Drawer-wide signals for the builds/jobs panel (W9): which job is shown, and counters that
// tell the lists to refetch (a job was submitted, or one finished and may have made a build).
import { writable } from 'svelte/store';

/** The job shown in the drawer's job view. */
export const selectedJob = writable<string | null>(null);
/** Bumped when a job is submitted from anywhere in the drawer. */
export const jobsVersion = writable(0);
/** Bumped when a job finishes (its build, if any, appears in the builds list). */
export const buildsVersion = writable(0);

export const bump = (store: typeof jobsVersion) => store.update((n) => n + 1);
/** Jobs still queued or running (JobsPanel polls; the header shows it). */
export const runningJobs = writable(0);
