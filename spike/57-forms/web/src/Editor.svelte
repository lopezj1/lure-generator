<script lang="ts">
  import {
    BasicForm,
    createForm,
    getValueSnapshot,
    updateErrors,
    type Schema,
    type UiSchemaRoot,
  } from "@sjsf/form";
  import { resolver } from "@sjsf/form/resolvers/compat";
  // The compat resolver returns "enumField" for enums but doesn't register it: import it.
  import "@sjsf/form/fields/extra/enum-include";
  import { translation } from "@sjsf/form/translations/en";
  import { createFormMerger } from "@sjsf/form/mergers/modern";
  import { createFormIdBuilder } from "@sjsf/form/id-builders/modern";
  import { createFormValidator } from "@sjsf/ajv8-validator";
  import { setContext } from "svelte";
  import { theme } from "./theme";

  let {
    schema,
    uiSchema,
    value,
    provenance,
  }: { schema: Schema; uiSchema: UiSchemaRoot; value: any; provenance: Record<string, string> } =
    $props();
  // svelte-ignore state_referenced_locally
  setContext("provenance", provenance);

  let status = $state("");
  let output = $state("");

  // svelte-ignore state_referenced_locally
  const form = createForm({
    theme,
    schema,
    uiSchema,
    resolver,
    translation,
    merger: createFormMerger,
    validator: createFormValidator,
    idBuilder: createFormIdBuilder,
    initialValue: value,
    onSubmit: async (v: any) => {
      // Client (Ajv) passed; now the server: pydantic model validators + resolve().
      output = JSON.stringify(v, null, 1);
      (window as any).__formOutput = v;
      const r = await fetch("/api/validate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(v),
      }).then((r) => r.json());
      if (r.ok) {
        status = "server: valid ✓ (round-trips through LureConfig.model_validate + resolve)";
        updateErrors(form, []);
      } else {
        status = `server: ${r.errors.length} error(s)`;
        updateErrors(form, r.errors);
      }
      (window as any).__serverResult = r;
    },
    onSubmitError: (res) => {
      status = `client (Ajv): ${res.errors.length} error(s)`;
      (window as any).__clientErrors = res.errors;
    },
  });
  (window as any).__snapshot = () => getValueSnapshot(form);
</script>

<BasicForm {form} novalidate />
<p id="status"><strong>{status}</strong></p>
{#if output}
  <details><summary>Submitted JSON</summary><pre>{output}</pre></details>
{/if}
