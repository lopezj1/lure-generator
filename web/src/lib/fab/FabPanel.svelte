<!-- Fabrication bar on the Print and CNC pages (#121): the current build, a machine choice
     (GET /api/machines), "Generate print G-code" / "Generate CNC job" as a `fab_print` /
     `fab_cnc` job (= `lure fab <build> --format print|cnc [--machine M]`), its progress and
     log, and the build's fab/print or fab/cnc downloads. A finished job reloads the preview
     (it follows `currentBuild`). -->
<script lang="ts">
  import { api } from '../client';
  import type { components } from '../api';
  import { buildsVersion, bump, jobsVersion } from '../drawer/state';
  import { bytes, stampTime } from '../drawer/format';
  import JobView from '../editor/JobView.svelte';
  import { errorText, submitJob, type JobStatus } from '../jobs';
  import { currentBuild, setCurrentBuild } from '../stores/build';
  import { go } from '../stores/route';
  import { selection } from '../stores/selection';

  type BuildSummary = components['schemas']['BuildSummary'];
  type Machine = components['schemas']['MachineSummary'];

  let { format }: { format: 'print' | 'cnc' } = $props();

  let builds: BuildSummary[] = $state([]);
  let machines: Machine[] = $state([]);
  let machine = $state('');
  let jobId: string | null = $state(null);
  let running = $state(false);
  let message = $state('');
  let showJob = $state(true);

  const kind = $derived(format === 'print' ? 'printer' : 'router');
  const prefix = $derived(format === 'print' ? 'fab/print/' : 'fab/cnc/');
  const target = $derived(
    $currentBuild
      ? { family: $currentBuild.family, variant: $currentBuild.variant }
      : { family: $selection.family, variant: $selection.variant },
  );
  const key = (b: { family: string; variant: string; stamp: string }) =>
    `${b.family}/${b.variant}/${b.stamp}`;
  const current = $derived.by(() => {
    const cb = $currentBuild;
    if (cb) {
      if (cb.stamp === 'latest') return builds.find((b) => b.latest) ?? null;
      return builds.find((b) => key(b) === key(cb)) ?? null;
    }
    return builds.find((b) => b.latest) ?? builds[0] ?? null;
  });
  const downloads = $derived(
    (current?.downloads ?? []).filter(
      (d) => d.label.startsWith(prefix) || d.label === `${format} files`,
    ),
  );
  const command = $derived(
    current
      ? `lure fab ${current.path} --format ${format}${machine ? ` --machine ${machine}` : ''}`
      : '',
  );
  const action = $derived(format === 'print' ? 'Generate print G-code' : 'Generate CNC job');
  const hasOutput = $derived(downloads.length > 0);

  async function refresh() {
    const { family, variant } = target;
    if (!family || !variant) return;
    const { data } = await api.GET('/api/builds', { params: { query: { family, variant } } });
    builds = data ?? [];
  }

  $effect(() => {
    void $buildsVersion;
    void target.family;
    void target.variant;
    void refresh();
  });

  $effect(() => {
    void api.GET('/api/machines').then(({ data }) => {
      machines = (data ?? []).filter((m) => m.type === kind);
    });
  });

  function pick(value: string) {
    const b = builds.find((x) => key(x) === value);
    if (b) setCurrentBuild(b.path);
  }

  async function generate() {
    if (!current) return;
    message = '';
    try {
      running = true;
      showJob = true;
      const op = format === 'print' ? 'fab_print' : 'fab_cnc';
      jobId = await submitJob({
        op,
        args: { build: current.path, machine: machine || null },
      });
      bump(jobsVersion);
    } catch (err) {
      running = false;
      message = (err as Error).message ?? errorText(err);
    }
  }

  function ondone(status: JobStatus, error: string | null) {
    running = false;
    bump(buildsVersion);
    if (status === 'succeeded' && current) {
      message = `${format === 'print' ? 'Print G-code' : 'CNC job'} ready`;
      showJob = false;
      setCurrentBuild(current.path); // the preview reloads this build's new files
    } else {
      message = `fab ${format} ${status}${error ? `: ${error}` : ''}`;
    }
  }
</script>

<div class="fab-bar" data-testid="fab-{format}">
  <label class="build">
    <small>Build</small>
    {#if builds.length}
      <select value={current ? key(current) : ''} onchange={(e) => pick(e.currentTarget.value)}>
        {#each builds as b (key(b))}
          <option value={key(b)}>
            {b.variant} · {stampTime(b.stamp)}{b.latest ? ' (latest)' : ''}
          </option>
        {/each}
      </select>
    {:else}
      <small class="muted">
        no builds of {target.variant || 'this variant'} —
        <button type="button" class="mini" onclick={() => go('design')}>build it first</button>
      </small>
    {/if}
  </label>
  {#if machines.length}
    <label class="machine">
      <small>{format === 'print' ? 'Printer' : 'Router'}</small>
      <select bind:value={machine}>
        <option value="">default</option>
        {#each machines as m (m.id)}<option value={m.id}>{m.name}</option>{/each}
      </select>
    </label>
  {/if}
  <button
    type="button"
    class="generate"
    disabled={!current || running}
    aria-busy={running}
    title={command}
    onclick={generate}
  >
    {hasOutput ? `Regenerate` : action}
  </button>
  <div class="downloads">
    {#if hasOutput}
      <small class="muted">Download</small>
      {#each downloads as d (d.href)}
        <a class="mini" href={d.href} download title={d.label}>
          {d.label.replace(prefix, '').replace(`${format} files`, 'all (zip)')}{#if d.size != null}
            <span class="size">{bytes(d.size)}</span>{/if}
        </a>
      {/each}
    {:else if current}
      <small class="muted">
        No {format === 'print' ? 'G-code' : 'NC files'} for this build yet.
      </small>
    {/if}
  </div>
  {#if command}<code class="cli" title="The equivalent CLI command">{command}</code>{/if}
  {#if message}<small class="message" class:error-text={!message.endsWith('ready')}>
      {message}
    </small>{/if}
  {#if jobId && showJob}
    <div class="fab-job"><JobView id={jobId} {ondone} compact /></div>
  {:else if jobId}
    <button type="button" class="mini" onclick={() => (showJob = true)}>show job</button>
  {/if}
</div>

<style>
  .fab-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.35rem 0.9rem;
    padding: 0.4rem 0.6rem;
    margin-bottom: 0.4rem;
    border: 1px solid var(--pico-muted-border-color);
    border-radius: var(--pico-border-radius);
    background: var(--pico-card-background-color);
    font-size: 0.85rem;
  }
  label {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin: 0;
    min-width: 0;
  }
  select {
    margin: 0;
    width: auto;
    min-width: 0;
    max-width: 22rem;
    padding-top: 0.2rem;
    padding-bottom: 0.2rem;
    font-size: 0.85rem;
  }
  .generate {
    margin: 0;
    width: auto;
    padding: 0.3rem 0.9rem;
    font-size: 0.88rem;
  }
  .downloads {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    align-items: center;
    min-width: 0;
  }
  .size {
    opacity: 0.7;
    margin-left: 0.2rem;
  }
  .cli {
    flex-basis: 100%;
    margin: 0;
    padding: 0;
    background: none;
  }
  .message {
    flex-basis: 100%;
  }
  .fab-job {
    flex-basis: 100%;
    margin: 0;
  }
  @media (max-width: 599px) {
    .build,
    .machine {
      flex: 1 1 100%;
    }
    .build select,
    .machine select {
      flex: 1;
      max-width: none;
    }
  }
</style>
