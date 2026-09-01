import { createTranslatable, interpolate } from '@dynamicforms/translatable';

/**
 * A failed request's response body. `detail` is always a plain string - unchanged from what it has
 * always been. `detail_code` and `detail_params` are additive, and appear only when the server has
 * registered `df_viewset_exception_handler` (see the Python side's `fastapi_viewsets.exceptions`)
 * for one of this package's own built-in errors; a view's own `raise HTTPException(status_code,
 * detail="...")` never carries them.
 */
export interface ApiErrorBody {
  detail: string;
  detail_code?: string;
  detail_params?: Record<string, unknown>;
}

/**
 * English defaults for every `detail_code` the server side raises on its own, keyed by that code
 * rather than by its English text. `{name}`-style placeholders match the keys `detail_params`
 * carries for that code.
 */
export const { strings: translatableStrings, translateStrings } = createTranslatable({
  not_found: 'Item with pk {pk} not found',
  session_expired: 'Session expired or invalid',
  not_authorized: 'Not authorized to perform this action',
  rate_limited: 'Rate limit exceeded',
  unsupported_list_shape: 'unsupported list shape "{shape}"; this endpoint offers {allowed}',
  cursor_unreadable: 'cursor is not readable: {error}',
  cursor_missing_position: 'cursor is not readable: no position in it',
  cursor_stale: 'this cursor was issued for a different ordering or filter - start from the first page',
  cursor_missing_keys: 'cursor has no value for ordering key(s): {missing}',
  cursor_value_mismatch: 'cursor value for "{name}" does not fit the field: {error}',
});

/**
 * A translated, interpolated message for a failed request - `body.detail` unchanged when
 * `detail_code` is absent (the server has not registered the handler, or this is a view's own
 * plain-string error) or names a code this table does not (yet) cover.
 */
export function translateApiError(body: ApiErrorBody): string {
  if (!body.detail_code) return body.detail;

  const template = (translatableStrings as Record<string, string>)[body.detail_code];
  if (template == null) return body.detail;

  const params = { ...body.detail_params };
  if (Array.isArray(params.allowed)) params.allowed = params.allowed.join(', ');
  if (Array.isArray(params.missing)) params.missing = params.missing.join(', ');

  return interpolate(template, params);
}
