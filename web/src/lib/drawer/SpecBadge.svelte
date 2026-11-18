<!-- "spec has uncommitted changes" badge: GET /api/spec/status (git status of spec/; the
     terminal twin is `git status -- spec`). Polls slowly and on window focus; hidden when the
     server has no git repository (e.g. the Docker image). -->
<script lang="ts">
  import { api } from '../client';
  import type { components } from '../api';

  type SpecStatus = components['schemas']['SpecStatus'];

  let status = $state<SpecStatus | null>(null);

  async function refresh() {
    const { data } = await api.GET('/api/spec/status');
    if (data) status = data;
  }

  $effect(() => {
    void refresh();
    const timer = setInterval(refresh, 15_000);
    const onfocus = () => void refresh();
    window.addEventListener('focus', onfocus);
    return () => {
      clearInterval(timer);
      window.removeEventListener('focus', onfocus);
    };
  });

  const title = $derived(
    status?.files.map((f) => `${f.status.trim() || '?'} ${f.path}`).join('\n') ?? '',
  );
</script>

{#if status?.git}
  {#if status.dirty}
    <small class="spec-badge dirty" {title} data-testid="spec-badge">
      spec: {status.files.length} uncommitted {status.files.length === 1 ? 'change' : 'changes'}
    </small>
  {:else}
    <small class="spec-badge" data-testid="spec-badge">spec clean</small>
  {/if}
{/if}

<style>
  .spec-badge {
    padding: 0.05rem 0.5rem;
    border-radius: 0.7rem;
    border: 1px solid var(--pico-muted-border-color);
    color: var(--pico-muted-color);
    white-space: nowrap;
  }
  .spec-badge.dirty {
    color: var(--pico-del-color);
    border-color: var(--pico-del-color);
    cursor: help;
  }
</style>
