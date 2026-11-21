<!-- Builds & jobs page (W9 #70, a page since #121): jobs with live log/progress/cancel, and the
     builds browser with report summaries, downloads and compare. -->
<script lang="ts">
  import BuildsPanel from './BuildsPanel.svelte';
  import JobsPanel from './JobsPanel.svelte';
  import { runningJobs } from './state';
  import './builds.css';
</script>

<div class="w9 builds-page" data-testid="builds-page">
  <section class="card"><BuildsPanel /></section>
  <section class="card"><JobsPanel onactive={(n) => runningJobs.set(n)} /></section>
</div>
