/**
 * The namespace of the error codes this package declares. A code the BE raises on this package's
 * behalf is `${ERROR_NAMESPACE}.<code>`, such as `fastapi-viewsets.not_found`.
 */
export const ERROR_NAMESPACE = 'fastapi-viewsets';

/**
 * The namespace of pydantic's error types. `toFieldErrors()` forwards pydantic's `type` as
 * `${ERROR_NAMESPACE_PYDANTIC}.<type>`, such as `pydantic.string_too_short`.
 */
export const ERROR_NAMESPACE_PYDANTIC = 'pydantic';

/** The full code of `code`, an error code this package declares. */
export function namespaced(code: string): string {
  return `${ERROR_NAMESPACE}.${code}`;
}
