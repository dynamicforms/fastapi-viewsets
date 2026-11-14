/**
 * The namespace of the error codes this package declares. A code the BE raises on this package's
 * behalf is `${ERROR_NAMESPACE}.<code>`, such as `fastapi-viewsets.not_found`.
 */
export const ERROR_NAMESPACE = 'fastapi-viewsets';

/**
 * The namespace of pydantic's error types. A BE field error from pydantic that does not become a
 * `FieldErrorCode` has the code `${ERROR_NAMESPACE_PYDANTIC}.<type>`, such as `pydantic.json_invalid`.
 * `pydantic.custom_error` is in this namespace as well.
 */
export const ERROR_NAMESPACE_PYDANTIC = 'pydantic';

/**
 * The namespace of Django's validation codes. A BE field error from Django that does not become a
 * `FieldErrorCode` has the code `${ERROR_NAMESPACE_DJANGO}.<code>`, such as `django.unique_for_date`.
 */
export const ERROR_NAMESPACE_DJANGO = 'django';

/** The full code of `code`, an error code this package declares. */
export function namespaced(code: string): string {
  return `${ERROR_NAMESPACE}.${code}`;
}
