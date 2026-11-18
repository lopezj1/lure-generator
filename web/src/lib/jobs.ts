// Submit CLI jobs and follow them over SSE (`/api/jobs`, W2 #63). Shared by the config
// editor (build, profile) and the builds/jobs panel (W9).
import { api } from './client';
import type { components } from './api';

type S = components['schemas'];
export type JobRequest = S['ProfileJob'] | S['BuildJob'] | S['FabPrintJob'] | S['FabCncJob'];
export type JobStatus = S['JobInfo']['status'];
export type JobProgress = S['JobProgress'];
export type LogData = S['LogData'];
export type StatusData = S['StatusData'];

export const FINAL_STATUSES: ReadonlySet<JobStatus> = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'interrupted',
]);

export interface JobHandlers {
  log?: (data: LogData, id: number) => void;
  progress?: (data: JobProgress, id: number) => void;
  result?: (data: Record<string, unknown>, id: number) => void;
  /** Called for every status change; the stream closes itself after a final one. */
  status?: (data: StatusData, id: number) => void;
}

/** `POST /api/jobs`; returns the job id or throws with the server's message. */
export async function submitJob(req: JobRequest): Promise<string> {
  const { data, error, response } = await api.POST('/api/jobs', { body: req });
  if (!data) throw new Error(errorText(error, response));
  return data.id;
}

/** `DELETE /api/jobs/{id}` (409 = already finished, ignored). */
export async function cancelJob(id: string): Promise<void> {
  const { error, response } = await api.DELETE('/api/jobs/{job_id}', {
    params: { path: { job_id: id } },
  });
  if (error && response.status !== 409) throw new Error(errorText(error, response));
}

/**
 * Follow a job's named SSE events (`log`, `progress`, `result`, `status`). EventSource resumes
 * by itself with `Last-Event-ID`; the stream is closed after a final status. Returns a
 * function that stops following.
 */
export function followJob(id: string, on: JobHandlers): () => void {
  const es = new EventSource(`/api/jobs/${encodeURIComponent(id)}/events`);
  const listen = <T>(name: string, fn?: (data: T, id: number) => void) =>
    es.addEventListener(name, (e) => {
      const ev = e as MessageEvent<string>;
      fn?.(JSON.parse(ev.data) as T, Number(ev.lastEventId));
    });
  listen('log', on.log);
  listen('progress', on.progress);
  listen('result', on.result);
  listen<StatusData>('status', (data, n) => {
    on.status?.(data, n);
    if (FINAL_STATUSES.has(data.status)) es.close();
  });
  return () => es.close();
}

/** Best-effort human text for an openapi-fetch error body. */
export function errorText(error: unknown, response?: Response): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail ?? error;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: { msg?: string; message?: string; loc?: unknown[]; path?: unknown[] }) => {
        const where = (d.path ?? d.loc ?? []).join('.');
        return `${where ? where + ': ' : ''}${d.message ?? d.msg ?? JSON.stringify(d)}`;
      })
      .join('; ');
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message);
  }
  return response ? `HTTP ${response.status} ${response.statusText}` : 'request failed';
}
