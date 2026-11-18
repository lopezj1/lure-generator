<script lang="ts">
  import Editor from "./Editor.svelte";

  const load = Promise.all([
    fetch("/api/schema").then((r) => r.json()),
    fetch("/api/config").then((r) => r.json()),
  ]);
</script>

<h1>LureConfig form spike (#57)</h1>
{#await load}
  <p aria-busy="true">Loading schema…</p>
{:then [s, c]}
  <Editor schema={s.schema} uiSchema={s.uiSchema} value={c.value} provenance={c.provenance} />
{:catch e}
  <p>Failed: {e}</p>
{/await}
