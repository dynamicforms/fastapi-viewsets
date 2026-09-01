import { createTranslatable, interpolate } from '@dynamicforms/translatable';

/**
 * The `detail` shape every error this package's server side raises puts in the HTTP response body.
 *
 * A host application's own `raise HTTPException(status_code, detail="...")` still puts a plain
 * string there — only the library's own built-in errors (not-found, session-expired,
 * not-authorized, rate-limited, an unsupported list shape, a rejected cursor) carry this shape.
 * Narrow with `isApiErrorDetail` before reading `code`/`params` off an error you did not raise
 * yourself.
 */
export interface ApiErrorDetail {
  /** The English default, fully interpolated - safe to show as-is with no translation layer. */
  message: string;
  /** A stable identifier, independent of `message`, to switch on or look up a translation for. */
  code: string;
  /** The raw values `message` was interpolated with, for translating and re-interpolating a template. */
  params: Record<string, unknown>;
}

export function isApiErrorDetail(detail: unknown): detail is ApiErrorDetail {
  return (
    typeof detail === 'object' &&
    detail !== null &&
    typeof (detail as ApiErrorDetail).message === 'string' &&
    typeof (detail as ApiErrorDetail).code === 'string'
  );
}

/**
 * English defaults for every `code` the server side raises on its own, keyed by that code rather
 * than by its English text. `{name}`-style placeholders match the keys `ApiErrorDetail.params`
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
 * A translated, interpolated message for a structured API error - the English default from the
 * server itself for a `code` this table does not (yet) cover, so a new server-side error code
 * degrades to its own English text rather than to nothing.
 */
export function translateApiError(detail: ApiErrorDetail): string {
  const template = (translatableStrings as Record<string, string>)[detail.code];
  if (template == null) return detail.message;

  const params = { ...detail.params };
  if (Array.isArray(params.allowed)) params.allowed = params.allowed.join(', ');
  if (Array.isArray(params.missing)) params.missing = params.missing.join(', ');

  return interpolate(template, params);
}
