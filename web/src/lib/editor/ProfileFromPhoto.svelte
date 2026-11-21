<!-- Photo → station table: POST /api/uploads?kind=photo, then a `profile` job
     (`lure profile <photo> --out uploads/profiles/<id>/profile.csv …`), then show the overlay
     for approval. "Use this profile" points the form at the CSV (profile.source = preset).
     The job has no @@result yet (#74), so the output paths come from the request. -->
<script lang="ts">
  import { errorText, submitJob, type JobStatus } from '../jobs';
  import { getAt, setAt } from './paths';
  import type { EditorState } from './state.svelte';
  import JobView from './JobView.svelte';

  let { editor }: { editor: EditorState } = $props();

  let file: File | null = $state(null);
  let scale: 'auto' | 'aruco' | 'length' = $state('auto');
  // svelte-ignore state_referenced_locally
  let length: number | null = $state(
    (getAt(editor.value, ['size', 'length_mm']) as number) ?? null,
  );
  let nose: 'left' | 'right' = $state('left');
  let busy = $state(false);
  let message = $state('');
  let jobId: string | null = $state(null);
  let out: string | null = $state(null);
  let overlay: string | null = $state(null);
  let applied = $state(false);

  async function run() {
    if (!file) return;
    busy = true;
    message = '';
    overlay = null;
    applied = false;
    jobId = null;
    try {
      const body = new FormData();
      body.append('file', file);
      const res = await fetch('/api/uploads?kind=photo', { method: 'POST', body });
      const upload = await res.json();
      if (!res.ok) throw new Error(errorText(upload, res));
      const folder = `uploads/profiles/${String(upload.id).slice(0, 16)}`;
      out = `${folder}/profile.csv`;
      jobId = await submitJob({
        op: 'profile',
        args: {
          image: upload.path,
          out,
          scale,
          length: scale !== 'aruco' && length ? length : null,
          nose,
          rembg: false,
        },
      });
    } catch (err) {
      busy = false;
      message = (err as Error).message;
    }
  }

  function ondone(status: JobStatus, error: string | null) {
    busy = false;
    if (status === 'succeeded' && out) {
      overlay = `/api/${out.replace(/profile\.csv$/, 'profile_debug.png')}?job=${jobId}`;
    } else {
      message = `profile job ${status}${error ? `: ${error}` : ''} (see the log)`;
    }
  }

  function use() {
    if (!out) return;
    setAt(editor.value, ['profile', 'source'], 'preset');
    setAt(editor.value, ['profile', 'preset'], out);
    applied = true;
  }
</script>

<details class="photo">
  <summary>Profile from a photo</summary>
  <div class="photo-body">
    <small>
      Side view, nose {nose}; scale from an ArUco marker or the known length. Writes a station CSV
      under <code>uploads/profiles/</code>.
    </small>
    <input
      type="file"
      accept="image/*"
      aria-label="side-profile photo"
      onchange={(e) => (file = (e.currentTarget as HTMLInputElement).files?.[0] ?? null)}
    />
    <div class="grid-2">
      <label>
        Scale
        <select bind:value={scale}>
          <option value="auto">auto</option>
          <option value="aruco">aruco</option>
          <option value="length">length</option>
        </select>
      </label>
      <label>
        Nose
        <select bind:value={nose}>
          <option value="left">left</option>
          <option value="right">right</option>
        </select>
      </label>
    </div>
    {#if scale !== 'aruco'}
      <label>
        Length (mm)
        <input type="number" min="1" step="any" bind:value={length} />
      </label>
    {/if}
    <button type="button" disabled={!file || busy} aria-busy={busy} onclick={run}>
      Extract profile
    </button>
    {#if message}<small class="error-text">{message}</small>{/if}
    {#if jobId}
      <JobView id={jobId} {ondone} />
    {/if}
    {#if overlay}
      <figure>
        <img src={overlay} alt="Extracted outline over the side view" />
        <figcaption><small><code>{out}</code></small></figcaption>
      </figure>
      <div class="grid-2">
        <button type="button" disabled={applied} onclick={use}>Use this profile</button>
        <button type="button" class="secondary outline" onclick={() => (overlay = null)}>
          Discard
        </button>
      </div>
      {#if applied}
        <small>
          Set <code>profile.source = preset</code> and <code>profile.preset</code>. Save to keep it.
        </small>
      {/if}
    {/if}
  </div>
</details>
