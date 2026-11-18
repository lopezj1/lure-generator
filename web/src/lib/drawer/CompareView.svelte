<!-- Two builds side by side: GET /api/builds/compare?a=…&b=… (deltas are b − a, computed by the
     server) plus the resolved.toml keys that differ. -->
<script lang="ts">
  import { api } from '../client';
  import type { components } from '../api';
  import { errorText } from '../jobs';
  import { num, signed, stampTime } from './format';

  type Comparison = components['schemas']['BuildComparison'];

  let { a, b, onclose }: { a: string; b: string; onclose: () => void } = $props();

  let cmp: Comparison | null = $state(null);
  let error = $state('');

  $effect(() => {
    const [ka, kb] = [a, b];
    cmp = null;
    error = '';
    void api
      .GET('/api/builds/compare', { params: { query: { a: ka, b: kb } } })
      .then(({ data, error: e, response }) => {
        if (data) cmp = data;
        else error = errorText(e, response);
      });
  });

  const digits = (unit: string) => (unit === 'g/cm³' || unit === 'oz' ? 3 : 2);
  const show = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v));
</script>

<div class="compare" data-testid="compare">
  <div class="head">
    <strong>Compare</strong>
    <button type="button" class="mini" onclick={onclose}>close</button>
  </div>
  {#if error}<small class="error-text">{error}</small>{/if}
  {#if cmp}
    <table class="compact">
      <thead>
        <tr>
          <th></th>
          <th class="num">A · {cmp.a.variant} {stampTime(cmp.a.stamp)}</th>
          <th class="num">B · {cmp.b.variant} {stampTime(cmp.b.stamp)}</th>
          <th class="num">Δ (B − A)</th>
        </tr>
      </thead>
      <tbody>
        {#each cmp.deltas as d (d.key)}
          <tr>
            <td>{d.label} <small>({d.unit})</small></td>
            <td class="num">{num(d.a, digits(d.unit))}</td>
            <td class="num">{num(d.b, digits(d.unit))}</td>
            <td class="num delta" class:pos={(d.delta ?? 0) > 0} class:neg={(d.delta ?? 0) < 0}>
              {signed(d.delta, digits(d.unit))}
            </td>
          </tr>
        {:else}
          <tr><td colspan="4"><small>Neither build has a mass report.</small></td></tr>
        {/each}
        <tr>
          <td>Verdict</td>
          <td class="num">{cmp.a.report?.verdict?.status ?? '–'}</td>
          <td class="num">{cmp.b.report?.verdict?.status ?? '–'}</td>
          <td></td>
        </tr>
      </tbody>
    </table>
    <details open={cmp.config_changes.length > 0 && cmp.config_changes.length <= 12}>
      <summary>
        <small>
          {cmp.config_changes.length}{cmp.config_changes_truncated ? '+' : ''} config
          {cmp.config_changes.length === 1 ? 'key differs' : 'keys differ'} (resolved.toml)
        </small>
      </summary>
      <table class="compact">
        <tbody>
          {#each cmp.config_changes as c (c.key)}
            <tr>
              <td><code>{c.key}</code></td>
              <td class="num">{show(c.a)}</td>
              <td class="num">{show(c.b)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </details>
  {:else if !error}
    <progress></progress>
  {/if}
</div>
