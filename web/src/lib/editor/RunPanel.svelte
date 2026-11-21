<!-- "Build": POST /api/jobs {op: "build", args: {variant}, overrides} = `lure build <variant>
     [--set k=v …]`. Overrides are temporary (this run only, never saved). On success the build
     becomes `currentBuild` for the viewers, with links to them. -->
<script lang="ts">
  import { bump, jobsVersion } from '../drawer/state';
  import { errorText, submitJob, type JobStatus } from '../jobs';
  import { go } from '../stores/route';
  import { setCurrentBuild } from '../stores/build';
  import JobView from './JobView.svelte';
  import { OVERRIDE_RE } from './paths';

  let { variantPath, dirty }: { variantPath: string; dirty: boolean } = $props();

  let rows: { key: string; value: string }[] = $state([]);
  let showOverrides = $state(false);
  let jobId: string | null = $state(null);
  let running = $state(false);
  let message = $state('');
  let builtDir: string | null = $state(null);
  let done = $state(false);

  const overrides = $derived(
    rows.filter((r) => r.key.trim()).map((r) => `${r.key.trim()}=${r.value.trim()}`),
  );
  const bad = $derived(overrides.filter((o) => !OVERRIDE_RE.test(o)));
  const command = $derived(
    ['lure build', variantPath, ...overrides.map((o) => `--set ${JSON.stringify(o)}`)].join(' '),
  );

  async function build() {
    message = '';
    builtDir = null;
    done = false;
    try {
      running = true;
      jobId = await submitJob({ op: 'build', args: { variant: variantPath }, overrides });
      bump(jobsVersion);
    } catch (err) {
      running = false;
      message = (err as Error).message ?? errorText(err);
    }
  }

  function onresult(data: Record<string, unknown>) {
    if (typeof data.build_dir === 'string') builtDir = data.build_dir;
  }

  function ondone(status: JobStatus) {
    running = false;
    if (status === 'succeeded' && builtDir) {
      setCurrentBuild(builtDir, jobId ?? undefined);
      message = `Built ${builtDir}`;
      done = true;
    } else if (status === 'succeeded') {
      message = 'build finished without a build_dir result';
    }
  }
</script>

<button
  type="button"
  class="build-btn"
  disabled={running || bad.length > 0}
  aria-busy={running}
  title={dirty ? `Builds the saved file (save first). ${command}` : command}
  onclick={build}
>
  ▶ Build
</button>
<button
  type="button"
  class="mini"
  aria-expanded={showOverrides}
  title="Temporary overrides for this build (--set)"
  onclick={() => (showOverrides = !showOverrides)}
>
  --set{overrides.length ? ` (${overrides.length})` : ''}
</button>

{#if showOverrides || jobId || message}
  <div class="run-panel">
    {#if dirty}
      <small class="warn-text">Unsaved edits: the build uses the saved file. Save first.</small>
    {/if}
    {#if showOverrides}
      <fieldset class="overrides">
        <legend><small>Temporary overrides (<code>--set</code>, not saved)</small></legend>
        {#each rows as row, i (i)}
          <div class="override-row">
            <input
              placeholder="buoyancy.target_sink"
              aria-label="override key"
              bind:value={row.key}
              aria-invalid={row.key && !OVERRIDE_RE.test(`${row.key}=${row.value}`)
                ? true
                : undefined}
            />
            <input placeholder="suspend" aria-label="override value" bind:value={row.value} />
            <button
              type="button"
              class="mini"
              aria-label="remove override"
              onclick={() => rows.splice(i, 1)}
            >
              ✕
            </button>
          </div>
        {/each}
        <button type="button" class="mini" onclick={() => rows.push({ key: '', value: '' })}>
          + override
        </button>
      </fieldset>
      {#if bad.length}<small class="error-text">Bad override: {bad.join(', ')}</small>{/if}
    {/if}
    <small class="cli" title="The equivalent CLI command"><code>{command}</code></small>
    {#if message}
      <small>
        {message}
        {#if done}
          · <button type="button" class="mini" onclick={() => go('model')}>Model</button>
          <button type="button" class="mini" onclick={() => go('print')}>Print</button>
          <button type="button" class="mini" onclick={() => go('cnc')}>CNC</button>
        {/if}
      </small>
    {/if}
    {#if jobId}
      <JobView id={jobId} {onresult} {ondone} />
    {/if}
  </div>
{/if}
