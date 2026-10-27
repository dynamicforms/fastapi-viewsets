/**
 * The list endpoint's `sort` parameter as a value (BE `SortState`, `parse_sort_param`). A `SortState`
 * has the shape of a `@dynamicforms/vue-grid` `sortState`.
 */

export interface SortStateColumn {
  columnName: string;
  direction: 'asc' | 'desc';
}

/** Columns in priority order. */
export type SortState = SortStateColumn[];

/**
 * Parses `'column:asc,other:desc'` as the BE does: a missing direction is `asc`; an empty entry or one
 * with another direction is skipped.
 */
export function parseSort(sort: string | null | undefined): SortState {
  const state: SortState = [];
  for (const raw of (sort ?? '').split(',')) {
    const entry = raw.trim();
    if (!entry) continue;
    const colon = entry.lastIndexOf(':');
    const columnName = colon > 0 ? entry.slice(0, colon) : entry;
    const direction = colon > 0 ? entry.slice(colon + 1) : 'asc';
    if (direction === 'asc' || direction === 'desc') state.push({ columnName, direction });
  }
  return state;
}

/** The `sort` parameter value of `state`; `''` for an empty state, which the BE reads as unordered. */
export function formatSort(state: readonly SortStateColumn[]): string {
  return state.map(({ columnName, direction }) => `${columnName}:${direction}`).join(',');
}
