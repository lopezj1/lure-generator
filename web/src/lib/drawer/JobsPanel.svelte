<!-- Jobs: running and recent jobs (GET /api/jobs), each with its CLI command; the selected one
     streams its log, progress and cancel through JobView (GET /api/jobs/{id}/events). -->
<script lang="ts">
  import { api } from '../client';
  import type { components } from '../api';
  import JobView from '../editor/JobView.svelte';
  import { cancelJob, FINAL_STATUSES } from '../jobs';
  import { setCurrentBuild } from '../stores/build';
  import { cliCommand, elapsed, jobTarget } from './format';
  import { buildsVersion, bump, jobsVersion, selectedJob } from './state';

  type JobInfo = components['schemas']['JobInfo'];

  let { onactive }: { onactive?: (running: number) => void } = $props();

  let jobs: JobInfo[] = $state([]);
  let error = $state('');
  let now = $state(Date.now());
  const seen: Record<string, string> = {}; // id → last status, to spot jobs that just finished

  const running = $derived(jobs.filter((j) => !FINAL_STATUSES.has(j.status)).length);
  const selected = $derived(jobs.find((j) => j.id === $selectedJob) ?? null);

  async function refresh() {
    const { data, error: e } = await api.GET('/api/jobs', { params: { query: { limit: 25 } } });
    if (e || !data) {
      error = 'could not list jobs';
      return;
    }
    error = '';
    let finished = false;
    for (const j of data) {
      const before = seen[j.id];
      if (
        before &&
        !FINAL_STATUSES.has(before as JobInfo['status']) &&
        FINAL_STATUSES.has(j.status)
      )
        finished = true;
      seen[j.id] = j.status;
    }
    jobs = data;
    now = Date.now();
    if (finished) bump(buildsVersion);
  }

  // Poll fast while something runs, slowly otherwise; refetch at once when a job is submitted.
  $effect(() => {
    void $jobsVersion;
    void refresh();
    const timer = setInterval(refresh, running ? 2000 : 10_000);
    return () => clearInterval(timer);
  });

  $effect(() => onactive?.(running));

  function buildDir(j: JobInfo): string | null {
    const dir = j.result?.build_dir;
    return typeof dir === 'string' ? dir : null;
  }

  const OPS: Record<string, string> = {
    build: 'build',
    profile: 'profile',
    fab_print: 'fab print',
    fab_cnc: 'fab cnc',
  };
</script>

<div class="jobs">
  <div class="head">
    <strong>Jobs</strong>
    <small>{running ? `${running} running` : 'idle'}</small>
    <button type="button" class="mini" onclick={refresh}>refresh</button>
  </div>
  {#if error}<small class="error-text">{error}</small>{/if}
  {#if !jobs.length}
    <p><small>No jobs yet. Build from the editor, or run fab from a build below.</small></p>
  {:else}
    <table class="compact">
      <colgroup>
        <col class="status" />
        <col class="op" />
        <col />
        <col class="time" />
        <col class="actions" />
      </colgroup>
      <tbody>
        {#each jobs as j (j.id)}
          {@const final = FINAL_STATUSES.has(j.status)}
          <tr
            class:selected={j.id === $selectedJob}
            onclick={() => selectedJob.set(j.id === $selectedJob ? null : j.id)}
            title={cliCommand(j.command)}
          >
            <td><span class="badge" data-status={j.status} aria-busy={!final}>{j.status}</span></td>
            <td>{OPS[j.op] ?? j.op}</td>
            <td class="target">{jobTarget(j.request)}</td>
            <td class="num">
              {#if !final && j.progress?.frac != null}
                {Math.round(j.progress.frac * 100)}%
              {:else}
                {elapsed(j.started_at, j.finished_at, now)}
              {/if}
            </td>
            <td class="actions">
              {#if !final}
                <button
                  type="button"
                  class="mini"
                  onclick={(e) => {
                    e.stopPropagation();
                    void cancelJob(j.id).then(refresh);
                  }}
                >
                  cancel
                </button>
              {:else if buildDir(j)}
                <button
                  type="button"
                  class="mini"
                  onclick={(e) => {
                    e.stopPropagation();
                    setCurrentBuild(buildDir(j)!, j.id);
                  }}
                >
                  open build
                </button>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
  {#if selected}
    <code class="cmd">{cliCommand(selected.command)}</code>
    <JobView id={selected.id} ondone={() => void refresh()} />
  {/if}
</div>
