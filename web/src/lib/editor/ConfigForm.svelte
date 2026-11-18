<!-- The schema-driven variant form (@sjsf/form). Its value is bound to `editor.value`, so the
     badges, resets and the save payload all work on the same state. -->
<script lang="ts">
  import { createFormValidator } from '@sjsf/ajv8-validator';
  import { BasicForm, createForm, updateErrors, type Schema, type UiSchemaRoot } from '@sjsf/form';
  // The compat resolver returns "enumField" for enums but doesn't register it: import it.
  import '@sjsf/form/fields/extra/enum-include';
  import { createFormIdBuilder } from '@sjsf/form/id-builders/modern';
  import { createFormMerger } from '@sjsf/form/mergers/modern';
  import { resolver } from '@sjsf/form/resolvers/compat';
  import { translation } from '@sjsf/form/translations/en';
  import { setEditor, type EditorState } from './state.svelte';
  import { theme } from './theme';

  type FieldError = { path: (string | number)[]; message: string };

  let {
    editor,
    schema,
    uiSchema,
    onsubmit,
  }: {
    editor: EditorState;
    schema: Schema;
    uiSchema: UiSchemaRoot;
    onsubmit: () => void;
  } = $props();

  // svelte-ignore state_referenced_locally
  setEditor(editor);

  // svelte-ignore state_referenced_locally
  const form = createForm<Record<string, unknown>>({
    theme,
    schema,
    uiSchema,
    resolver,
    translation,
    merger: createFormMerger,
    validator: createFormValidator,
    idBuilder: createFormIdBuilder,
    idPrefix: 'cfg',
    value: [() => editor.value, (v) => (editor.value = v)],
    initialErrors: editor.detail.errors.map((e) => ({ path: e.path, message: e.message })),
    onSubmit: () => onsubmit(),
    onSubmitError: () => onsubmit(),
  });

  /** Show server errors (`/validate`, 422 on save) inline; `[]` clears them. */
  export function showErrors(errors: FieldError[]) {
    updateErrors(
      form,
      errors.filter((e) => e.path.length > 0).map((e) => ({ path: e.path, message: e.message })),
    );
  }
</script>

<BasicForm {form} novalidate />
