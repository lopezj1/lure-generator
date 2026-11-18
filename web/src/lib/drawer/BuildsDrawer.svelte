<!-- Bottom drawer (W9, #70): jobs with live log/progress/cancel, and the builds browser with
     report summaries, downloads and compare. Opens by itself when a job starts. -->
<script lang="ts">
  import BuildsPanel from './BuildsPanel.svelte';
  import JobsPanel from './JobsPanel.svelte';
  import { jobsVersion, selectedJob } from './state';
  import './builds.css';

  let open = $state(false);
  let running = $state(0);

  // A job submitted from the drawer (or shown on request) opens it.
  $effect(() => {
    if ($jobsVersion > 0 && $selectedJob) open = true;
  });
</script>

<details class="w9" bind:open data-testid="drawer">
  <summary>
    Jobs and builds
    {#if running}<small class="badge" data-status="running" aria-busy="true">
        {running} running
      </small>{/if}
  </summary>
  <div class="drawer-grid">
    <section><JobsPanel onactive={(n) => (running = n)} /></section>
    <section><BuildsPanel /></section>
  </div>
</details>
