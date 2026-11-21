<!-- A free-key map (`x-layout: "keyvalue"`: print.overrides, solver.weights) as key/value
     rows (#121). Values are parsed loosely (number, true/false, else text) unless the schema
     says numbers. Renaming a key keeps its position. -->
<script lang="ts">
  import type { Path } from '../paths';
  import { getEditor } from '../state.svelte';
  import FieldMeta from './FieldMeta.svelte';
  import { errorsUnder, labelOf, parseLoose, type Node } from './schema';

  let { path, node }: { path: Path; node: Node } = $props();
  const editor = getEditor();

  const entries = $derived(
    Object.entries((editor.get(path) as Record<string, unknown> | undefined) ?? {}),
  );
  const valueNode = $derived((node.additionalProperties ?? {}) as Node);
  const numeric = $derived(valueNode.type === 'number' || valueNode.type === 'integer');
  const errors = $derived(errorsUnder(editor.errors, path).map((e) => e.message));

  function write(next: [string, unknown][]) {
    editor.set(path, Object.fromEntries(next));
  }
  function rename(i: number, key: string) {
    const k = key.trim();
    if (!k || entries.some(([other], j) => j !== i && other === k)) return;
    write(entries.map(([ek, v], j) => [j === i ? k : ek, v]));
  }
  function setValue(i: number, text: string) {
    const v = numeric ? Number(text) : parseLoose(text);
    write(entries.map(([k, old], j) => [k, j === i ? v : old]));
  }
  function add() {
    let n = entries.length + 1;
    while (entries.some(([k]) => k === `key_${n}`)) n++;
    write([...entries, [`key_${n}`, numeric ? 1 : '']]);
  }
  function remove(i: number) {
    write(entries.filter((_, j) => j !== i));
  }
</script>

<div class="kv-table">
  <div class="table-head">
    <span class="title" title={node.description ?? ''}>{labelOf(node, String(path.at(-1)))}</span>
    <FieldMeta {path} {node} />
  </div>
  {#if node.description}<small class="muted">{node.description}</small>{/if}
  {#if entries.length}
    <div class="scroll">
      <table>
        <thead><tr><th>Key</th><th>Value</th><th></th></tr></thead>
        <tbody>
          {#each entries as [k, v], i (i)}
            <tr>
              <td>
                <input
                  aria-label="key"
                  value={k}
                  onchange={(e) => rename(i, e.currentTarget.value)}
                />
              </td>
              <td>
                <input
                  aria-label="value of {k}"
                  type={numeric ? 'number' : 'text'}
                  step="any"
                  value={String(v ?? '')}
                  onchange={(e) => setValue(i, e.currentTarget.value)}
                />
              </td>
              <td class="ops">
                <button
                  type="button"
                  class="mini"
                  aria-label="Remove {k}"
                  onclick={() => remove(i)}
                >
                  ✕
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
  {#each errors as e (e)}<small class="error-text">{e}</small>{/each}
  <button type="button" class="mini add" onclick={add}>+ Add entry</button>
</div>
