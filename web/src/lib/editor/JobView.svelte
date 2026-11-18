<!-- Live view of one job: status, progress bar, log tail and a cancel button. Follows the
     job's SSE stream (lib/jobs.ts) and reports its result and final status upward. -->
<script lang="ts">
  import { cancelJob, followJob, FINAL_STATUSES, type JobProgress, type JobStatus } from '../jobs';

  let {
    id,
    onresult,
    ondone,
  }: {
    id: string;
    onresult?: (data: Record<string, unknown>) => void;
    ondone?: (status: JobStatus, error: string | null) => void;
  } = $props();

  let status: JobStatus = $state('queued');
  let error: string | null = $state(null);
  let progress: JobProgress | null = $state(null);
  let lines: string[] = $state([]);
  let pre: HTMLPreElement | undefined = $state();
  // A derived compares by value: a parent passing `job.id` from a refreshed object must not
  // restart the stream (and replay the whole log) on every refresh.
  const jobId = $derived(id);

  $effect(() => {
    const current = jobId;
    lines = [];
    status = 'queued';
    progress = null;
    error = null;
    return followJob(current, {
      log: (d) => {
        lines.push(d.line);
        if (lines.length > 500) lines.splice(0, lines.length - 500);
      },
      progress: (d) => (progress = d),
      result: (d) => onresult?.(d),
      status: (d) => {
        status = d.status;
        error = d.error ?? null;
        if (FINAL_STATUSES.has(d.status)) ondone?.(d.status, error);
      },
    });
  });

  $effect(() => {
    void lines.length;
    if (pre) pre.scrollTop = pre.scrollHeight;
  });

  const running = $derived(!FINAL_STATUSES.has(status));
</script>

<div class="job">
  <div class="job-head">
    <small>
      job <code>{id}</code> ·
      <span class="badge" data-status={status} aria-busy={running}>{status}</span>
    </small>
    {#if running}
      <button type="button" class="mini" onclick={() => cancelJob(id)}>cancel</button>
    {/if}
  </div>
  {#if progress}
    <small>{progress.stage}{progress.msg ? `: ${progress.msg}` : ''}</small>
    <progress value={progress.frac ?? undefined} max="1"></progress>
  {:else if running}
    <progress></progress>
  {/if}
  {#if error}<small class="error-text">{error}</small>{/if}
  <pre bind:this={pre} class="job-log">{lines.join('\n')}</pre>
</div>
