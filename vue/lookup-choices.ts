/*
 * A ViewSet's `lookup()` as the `fetchChoices` of a `@dynamicforms/vuetify-inputs` `df-select`. The types
 * have the shapes of its `SelectChoice` and `SelectFetchChoices`; this package does not import it.
 */

import type { KeyType, LookupItem, LookupParams } from './mixins';

/** One choice of a select; the shape of a `@dynamicforms/vuetify-inputs` `SelectChoice`. */
export interface LookupChoice {
  id: any;
  text: any;
  icon?: string;
}

/**
 * Choices for a select: for `queryValue`, the search text, or for `idValue`, one primary key or an array of
 * them. Assignable to a `@dynamicforms/vuetify-inputs` `SelectFetchChoices`.
 */
export type FetchLookupChoices = (queryValue?: string, idValue?: KeyType | KeyType[] | null) => Promise<LookupChoice[]>;

/** The lookup parameters `lookupChoices` sends: `{ q }` for a search, `{ ids }` for primary keys. */
export type LookupChoicesParams = Pick<LookupParams, 'q' | 'ids'>;

/** An object with a `lookup()` answering items of type `L`, e.g. a ViewSet declaring `LookupMixin<L>`. */
export interface LookupChoicesSource<L> {
  lookup(params: LookupChoicesParams): PromiseLike<L[]>;
}

/** A `LookupItem` as a choice: `pk` → `id`, `title` → `text`, `icon` → `icon` (omitted when null). */
function lookupItemChoice(item: LookupItem): LookupChoice {
  const choice: LookupChoice = { id: item.pk, text: item.title };
  if (item.icon != null) choice.icon = item.icon;
  return choice;
}

/**
 * Builds a `fetchChoices` over a lookup. With `idValue` set it sends `{ ids }`, resolving `[]` without a
 * request for an empty array; otherwise `{ q: queryValue }`, without `q` when `queryValue` is empty.
 *
 * - `lookupChoices(viewset)`: a ViewSet whose lookup answers `LookupItem`s.
 * - `lookupChoices(viewset, toChoice)`: any lookup item type, converted by `toChoice`.
 * - `lookupChoices(lookup, toChoice)`: `lookup(params)` makes the call, e.g. to add fixed parameters.
 *
 * @example
 * const fetchTagChoices = lookupChoices(tagsApi, (tag) => ({ id: tag.id, text: tag.name }));
 * // <df-select :control="form.fields.tags" :fetch-choices="fetchTagChoices" multiple />
 */
export function lookupChoices(viewset: LookupChoicesSource<LookupItem>): FetchLookupChoices;
export function lookupChoices<L>(
  viewset: LookupChoicesSource<L>,
  toChoice: (item: L) => LookupChoice,
): FetchLookupChoices;
export function lookupChoices<L>(
  lookup: (params: LookupChoicesParams) => PromiseLike<L[]>,
  toChoice: (item: L) => LookupChoice,
): FetchLookupChoices;
export function lookupChoices(
  source: LookupChoicesSource<unknown> | ((params: LookupChoicesParams) => PromiseLike<unknown[]>),
  toChoice: (item: any) => LookupChoice = lookupItemChoice,
): FetchLookupChoices {
  // A ViewSet is an object, never a function; `lookup` is called on it to keep `this`.
  const lookup = typeof source === 'function' ? source : (params: LookupChoicesParams) => source.lookup(params);
  return async (queryValue, idValue) => {
    let params: LookupChoicesParams;
    if (idValue != null) {
      const ids = Array.isArray(idValue) ? idValue : [idValue];
      if (!ids.length) return [];
      params = { ids };
    } else {
      params = queryValue ? { q: queryValue } : {};
    }
    const items = await lookup(params);
    return items.map((item) => toChoice(item));
  };
}
