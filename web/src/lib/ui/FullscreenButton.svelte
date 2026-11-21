<!-- Fullscreen toggle for a preview container (#121). Uses the Fullscreen API on `target`;
     where it is missing (iPhone Safari) the container becomes a fixed full-viewport overlay.
     Esc leaves either mode. The viewers resize through their own ResizeObservers. The target
     gets the `fs-host` class so ui.css can give it a background in fullscreen. -->
<script lang="ts">
  let { target }: { target: HTMLElement | undefined } = $props();

  let on = $state(false);
  let pseudo = $state(false);

  $effect(() => {
    target?.classList.add('fs-host');
  });

  function sync() {
    on = pseudo || (!!target && document.fullscreenElement === target);
  }

  function setPseudo(value: boolean) {
    pseudo = value;
    target?.classList.toggle('pseudo-fullscreen', value);
    sync();
  }

  async function toggle() {
    if (!target) return;
    if (on) {
      if (pseudo) setPseudo(false);
      else if (document.fullscreenElement) await document.exitFullscreen();
      return;
    }
    if (document.fullscreenEnabled && target.requestFullscreen) {
      try {
        await target.requestFullscreen();
        return;
      } catch {
        // fall through to the overlay
      }
    }
    setPseudo(true);
  }

  function onkey(e: KeyboardEvent) {
    if (pseudo && e.key === 'Escape') {
      e.preventDefault();
      setPseudo(false);
    }
  }
</script>

<svelte:document onfullscreenchange={sync} />
<svelte:window onkeydown={onkey} />

<button
  type="button"
  class="fs-btn"
  aria-pressed={on}
  title={on ? 'Exit fullscreen (Esc)' : 'Fullscreen'}
  aria-label={on ? 'Exit fullscreen' : 'Fullscreen'}
  onclick={toggle}
>
  {on ? '✕ Exit fullscreen' : 'Fullscreen'}
</button>
