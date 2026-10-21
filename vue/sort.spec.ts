import { describe, expect, it } from 'vitest';

import { formatSort, parseSort } from './sort';

describe('parseSort', () => {
  it('reads entries in priority order, asc when the direction is omitted', () => {
    expect(parseSort('year:desc,title')).toEqual([
      { columnName: 'year', direction: 'desc' },
      { columnName: 'title', direction: 'asc' },
    ]);
  });

  it('skips empty entries and entries with an unrecognised direction, as the BE does', () => {
    expect(parseSort(' title:up, ,id:asc ')).toEqual([{ columnName: 'id', direction: 'asc' }]);
  });

  it('splits at the last colon', () => {
    expect(parseSort('a:b:desc')).toEqual([{ columnName: 'a:b', direction: 'desc' }]);
  });

  it('reads nothing from an empty or missing value', () => {
    expect(parseSort('')).toEqual([]);
    expect(parseSort(undefined)).toEqual([]);
    expect(parseSort(null)).toEqual([]);
  });
});

describe('formatSort', () => {
  it('writes the sort parameter that parseSort reads back', () => {
    const state = [
      { columnName: 'year', direction: 'desc' as const },
      { columnName: 'title', direction: 'asc' as const },
    ];
    expect(formatSort(state)).toBe('year:desc,title:asc');
    expect(parseSort(formatSort(state))).toEqual(state);
  });

  it("writes '' for an empty state", () => {
    expect(formatSort([])).toBe('');
  });
});
