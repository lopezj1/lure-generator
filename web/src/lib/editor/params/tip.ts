// Hover/focus tooltips for parameter labels (#132). One shared, fixed-position bubble, so
// table scrollers and cards never clip it; it shows on pointer hover of the element and on
// keyboard focus inside `focusWithin` (e.g. the field row that holds the label's input).
// The text is the field's schema `description`; `sub` adds a muted second line (origin).

export interface TipOptions {
  text?: string;
  sub?: string;
  /** Also show while focus is inside the closest ancestor matching this selector. */
  focusWithin?: string;
}

const SHOW_DELAY_MS = 200;
let bubble: HTMLDivElement | null = null;
let timer: ReturnType<typeof setTimeout> | undefined;
let owner: HTMLElement | null = null;

function el(): HTMLDivElement {
  if (!bubble) {
    bubble = document.createElement('div');
    bubble.id = 'param-tip';
    bubble.className = 'param-tip';
    bubble.setAttribute('role', 'tooltip');
    bubble.hidden = true;
    document.body.append(bubble);
    window.addEventListener('scroll', () => owner && hide(owner), true);
  }
  return bubble;
}

function place(anchor: HTMLElement) {
  const b = el();
  const r = anchor.getBoundingClientRect();
  const gap = 6;
  const w = b.offsetWidth;
  const h = b.offsetHeight;
  const left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
  const above = r.top - h - gap;
  b.style.left = `${left}px`;
  b.style.top = `${above >= 8 ? above : r.bottom + gap}px`;
}

function show(anchor: HTMLElement, opts: TipOptions) {
  if (!opts.text && !opts.sub) return;
  const b = el();
  b.replaceChildren();
  if (opts.text) b.append(opts.text);
  if (opts.sub) {
    const s = document.createElement('small');
    s.textContent = opts.sub;
    b.append(s);
  }
  b.hidden = false;
  owner = anchor;
  place(anchor);
}

function hide(anchor: HTMLElement) {
  clearTimeout(timer);
  if (owner === anchor && bubble) {
    bubble.hidden = true;
    owner = null;
  }
}

export function tip(node: HTMLElement, initial: TipOptions) {
  let opts = initial;
  const focusRoot = () =>
    (opts.focusWithin ? node.closest<HTMLElement>(opts.focusWithin) : null) ?? node;

  const enter = () => {
    clearTimeout(timer);
    timer = setTimeout(() => show(node, opts), SHOW_DELAY_MS);
  };
  const leave = () => hide(node);
  const focusIn = (e: FocusEvent) => {
    const target = e.target as HTMLElement;
    target.setAttribute('aria-describedby', 'param-tip');
    // Keyboard focus only: a mouse click focuses too, and the hover already showed it.
    if (target.matches(':focus-visible')) show(node, opts);
  };
  const focusOut = (e: FocusEvent) => {
    (e.target as HTMLElement).removeAttribute('aria-describedby');
    hide(node);
  };
  const key = (e: KeyboardEvent) => e.key === 'Escape' && hide(node);

  const root = focusRoot();
  node.addEventListener('mouseenter', enter);
  node.addEventListener('mouseleave', leave);
  root.addEventListener('focusin', focusIn);
  root.addEventListener('focusout', focusOut);
  root.addEventListener('keydown', key);

  return {
    update(next: TipOptions) {
      opts = next;
      if (owner === node) show(node, opts);
    },
    destroy() {
      hide(node);
      node.removeEventListener('mouseenter', enter);
      node.removeEventListener('mouseleave', leave);
      root.removeEventListener('focusin', focusIn);
      root.removeEventListener('focusout', focusOut);
      root.removeEventListener('keydown', key);
    },
  };
}
