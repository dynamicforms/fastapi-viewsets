import { namespaced } from './error-namespace';

/**
 * One failure of one value of a request that failed validation: pydantic's error type in the
 * namespace `ERROR_NAMESPACE_PYDANTIC` as `code` (`pydantic.string_too_short`), the values pydantic
 * built its message from as `params`, and pydantic's English message as `detail`. A type pydantic
 * does not declare is `pydantic.custom_error` with `params` `{ type, ctx }`.
 */
export interface ApiFieldError {
  code: string;
  params: Record<string, unknown>;
  detail: string;
}

/**
 * The body of every error response of the BE. `detail` is English text, `detail_code` the full code
 * (`<namespace>.<code>`: `fastapi-viewsets.<code>` for the errors the package raises, the
 * application's own code as given for its own errors), `detail_params` the values `detail` was
 * interpolated with. `field_errors` holds the failures of a request that failed validation
 * (`fastapi-viewsets.validation_failed`) by field name, and is `{}` for every other error.
 */
export interface ApiErrorBody {
  detail: string;
  detail_code: string;
  detail_params: Record<string, unknown>;
  field_errors: Record<string, ApiFieldError[]>;
}

/**
 * An error a server returned: what failed, the values it failed with, its English text and its
 * origin, which is always `'server'`. It is assignable to `ErrorDescription` of
 * `@dynamicforms/vue-forms`, so an application renders and translates it with the same function as
 * its validators' errors.
 */
export interface ErrorDescription {
  readonly code: string;
  readonly params: Readonly<Record<string, unknown>>;
  readonly detail: string;
  readonly origin: 'server';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Whether `data` is an `ApiErrorBody`: `detail` and `detail_code` strings, `detail_params` and
 * `field_errors` objects.
 */
export function isApiErrorBody(data: unknown): data is ApiErrorBody {
  return (
    isRecord(data) &&
    typeof data.detail === 'string' &&
    typeof data.detail_code === 'string' &&
    isRecord(data.detail_params) &&
    isRecord(data.field_errors)
  );
}

/**
 * The `ApiErrorBody` of what a call threw: the body of its response (`error.response.data`, as both
 * `AxiosError` and `ViewSetRequestError` carry it) when that is an `ApiErrorBody`. A call the BE did
 * not answer has none, and the client builds it here:
 *
 * - `fastapi-viewsets.no_response`: the error carries no `response` (a network failure, a timeout, a
 *   cancelled request, a muxws stream that was reset or lost its connection, any other exception);
 * - `fastapi-viewsets.unexpected_response`, `detail_params` `{ status }`: the response's body is not an
 *   `ApiErrorBody` (a proxy's HTML 502, a route of the application without `install_error_handlers`).
 */
export function errorBodyOf(error: unknown): ApiErrorBody {
  const response = (error as { response?: unknown } | null | undefined)?.response;
  if (!isRecord(response)) {
    return {
      detail: 'The server did not answer',
      detail_code: namespaced('no_response'),
      detail_params: {},
      field_errors: {},
    };
  }
  if (isApiErrorBody(response.data)) return response.data;
  const status = typeof response.status === 'number' ? response.status : null;
  return {
    detail: `The server answered with status ${status ?? 'unknown'} and a body that is not an error body`,
    detail_code: namespaced('unexpected_response'),
    detail_params: { status },
    field_errors: {},
  };
}

/**
 * The error an `ApiErrorBody` describes: `detail_code` as `code`, `detail_params` as `params` and
 * `detail` unchanged.
 */
export function toErrorDescription(body: ApiErrorBody): ErrorDescription {
  return {
    code: body.detail_code,
    params: body.detail_params,
    detail: body.detail,
    origin: 'server',
  };
}

/**
 * The `field_errors` of an `ApiErrorBody` as errors keyed by field name: `loc` of pydantic's error
 * without its first element, joined with `.` (`address.city`); a failure of the body as a whole is
 * keyed `''`. `{}` for a body without field errors.
 */
export function toFieldErrors(body: ApiErrorBody): Record<string, ErrorDescription[]> {
  const errors: Record<string, ErrorDescription[]> = {};
  for (const [field, entries] of Object.entries(body.field_errors)) {
    errors[field] = entries.map((entry) => ({
      code: entry.code,
      params: entry.params,
      detail: entry.detail,
      origin: 'server',
    }));
  }
  return errors;
}

/**
 * What a failed call throws over muxws, where there is no axios to raise anything.
 *
 * The shape mirrors `AxiosError` — `error.response.status`, `error.response.data` and
 * `error.response.headers` — so a caller reads the same fields whichever transport the ViewSet
 * speaks. Over HTTP axios raises its own error, already in that shape, and it is passed through
 * untouched.
 *
 * `response` is always set here, unlike `AxiosError.response`, which is absent when the request
 * never reached a reply.
 */
export class ViewSetRequestError extends Error {
  readonly response: { status: number; data: unknown; headers: Record<string, string> };

  constructor(status: number, data: unknown, headers: Record<string, string> = {}) {
    super(`Request failed with status code ${status}`);
    this.name = 'ViewSetRequestError';
    this.response = { status, data, headers };
  }
}
