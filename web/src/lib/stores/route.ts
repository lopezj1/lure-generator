// The page shown, kept in the URL hash (`#/print`, `#/design/hardware`) so reload and
// back/forward work (#121). `page` is the top-level tab, `sub` an optional sub-tab.
import { writable } from 'svelte/store';

export const PAGES = ['design', 'model', 'print', 'cnc', 'builds'] as const;
export type Page = (typeof PAGES)[number];

export interface Route {
  page: Page;
  sub: string;
}

export function parseHash(hash: string): Route {
  const [page, sub = ''] = hash.replace(/^#\/?/, '').split('/');
  return { page: (PAGES as readonly string[]).includes(page) ? (page as Page) : 'design', sub };
}

function hashOf(r: Route): string {
  return `#/${r.page}${r.sub ? `/${r.sub}` : ''}`;
}

export const route = writable<Route>(
  typeof location === 'undefined' ? { page: 'design', sub: '' } : parseHash(location.hash),
);

if (typeof window !== 'undefined') {
  window.addEventListener('hashchange', () => route.set(parseHash(location.hash)));
}

const lastSub: Partial<Record<Page, string>> = {};
route.subscribe((r) => (lastSub[r.page] = r.sub));

/** Go to a page; without `sub` it returns to the sub-tab last shown there. */
export function go(page: Page, sub?: string): void {
  const next = hashOf({ page, sub: sub ?? lastSub[page] ?? '' });
  if (location.hash !== next) location.hash = next;
}

/** Change the sub-tab of a page without adding a history entry per click. */
export function setSub(page: Page, sub: string): void {
  const next = hashOf({ page, sub });
  if (location.hash !== next) history.replaceState(null, '', next);
  route.set({ page, sub });
}
