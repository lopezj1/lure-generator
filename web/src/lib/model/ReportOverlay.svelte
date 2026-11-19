<script lang="ts">
  // Key numbers from the build's report.json. Renders whatever exists: today the envelope
  // (volume, solid mass); per-section masses, CG/CB and the verdict once F3 writes them.
  type Json = Record<string, unknown>;

  let { base }: { base: string } = $props();

  let report: Json | null = $state(null);
  let open = $state(true);

  $effect(() => {
    const ctrl = new AbortController();
    report = null;
    fetch(`${base}/files/report.json`, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => (report = j && typeof j === 'object' ? (j as Json) : null))
      .catch(() => {});
    return () => ctrl.abort();
  });

  const G_PER_OZ = 28.349523125;
  const obj = (v: unknown): Json | null =>
    v && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null;
  const num = (v: unknown): number | null =>
    typeof v === 'number' && Number.isFinite(v) ? v : null;
  const vec = (v: unknown): string | null =>
    Array.isArray(v) && v.every((x) => typeof x === 'number')
      ? `(${(v as number[]).map((x) => x.toFixed(1)).join(', ')}) mm`
      : null;
  const mass = (g: number | null) =>
    g == null ? null : `${g.toFixed(1)} g · ${(g / G_PER_OZ).toFixed(2)} oz`;

  /** First value under any of `keys`, searched in report, then its physics-like blocks. */
  function pick(keys: string[]): unknown {
    if (!report) return undefined;
    const scopes = [
      report,
      ...['physics', 'balance', 'buoyancy', 'summary'].map((k) => obj(report![k])),
    ];
    for (const s of scopes) {
      if (!s) continue;
      for (const k of keys) if (s[k] !== undefined) return s[k];
    }
    return undefined;
  }

  const rows = $derived.by(() => {
    if (!report) return [];
    const out: [string, string][] = [];
    const env = obj(report.envelope);
    if (env) {
      const v = num(env.volume_cm3);
      if (v != null) out.push(['Envelope volume', `${v.toFixed(2)} cm³`]);
      const m = mass(num(env.solid_mass_g));
      if (m) out.push(['If solid (no cavity)', `${m} @ ${num(env.material_density) ?? '?'} g/cm³`]);
    }
    const total = mass(num(pick(['mass_g', 'total_mass_g'])));
    if (total) out.push(['Total mass', total]);
    const cg = vec(pick(['cg_mm', 'cg']));
    if (cg) out.push(['CG', cg]);
    const cb = vec(pick(['cb_mm', 'cb']));
    if (cb) out.push(['CB', cb]);
    const verdict = pick(['verdict']);
    if (typeof verdict === 'string') out.push(['Verdict', verdict]);
    else if (obj(verdict)) {
      const vv = obj(verdict)!;
      out.push(['Verdict', String(vv.label ?? vv.status ?? vv.result ?? JSON.stringify(vv))]);
    }
    const sections = obj(report.sections);
    for (const [name, s] of Object.entries(sections ?? {})) {
      const so = obj(s);
      if (!so) continue;
      const parts = [
        mass(num(so.mass_g)),
        num(so.volume_cm3) != null ? `${num(so.volume_cm3)!.toFixed(2)} cm³` : null,
        num(so.density_g_cm3) != null ? `ρ ${num(so.density_g_cm3)!.toFixed(3)}` : null,
      ].filter(Boolean);
      if (parts.length) out.push([`Section ${name}`, parts.join(' · ')]);
    }
    return out;
  });
</script>

{#if report && rows.length}
  <div class="overlay">
    <button class="toggle outline secondary" onclick={() => (open = !open)}>
      Report {open ? '▾' : '▸'}
      {#if typeof report.status === 'string'}<small>({report.status})</small>{/if}
    </button>
    {#if open}
      <table>
        <tbody>
          {#each rows as [k, v] (k)}
            <tr><th>{k}</th><td>{v}</td></tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
{/if}

<style>
  .overlay {
    position: absolute;
    right: 0.5rem;
    bottom: 0.5rem;
    z-index: 5;
    max-width: 20rem;
    pointer-events: auto;
    font-size: 0.75rem;
    background: color-mix(in srgb, var(--pico-background-color) 88%, transparent);
    border: 1px solid var(--pico-muted-border-color);
    border-radius: var(--pico-border-radius);
    padding: 0.25rem 0.5rem;
  }
  .toggle {
    padding: 0.1rem 0.4rem;
    font-size: 0.75rem;
    margin: 0;
    border: none;
  }
  table {
    margin: 0;
  }
  th,
  td {
    padding: 0.1rem 0.4rem;
    background: transparent;
    font-size: 0.75rem;
    white-space: nowrap;
  }
</style>
