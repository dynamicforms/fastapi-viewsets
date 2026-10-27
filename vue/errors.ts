import { namespaced } from './error-namespace';

/**
 * One validation failure: `code` is `pydantic.<type>` (unknown types `pydantic.custom_error` with
 * `params` `{ type, ctx }`), `params` pydantic's `ctx`, `detail` its English message.
 */
export interface ApiFieldError {
  code: string;
  params: Record<string, unknown>;
  detail: string;
}

/**
 * The body of every BE error response: English `detail`, `detail_code` as `<namespace>.<code>`, its
 * `detail_params`, and `field_errors` by field name (`{}` unless `fastapi-viewsets.validation_failed`).
 */
export interface ApiErrorBody {
  detail: string;
  detail_code: string;
  detail_params: Record<string, unknown>;
  field_errors: Record<string, ApiFieldError[]>;
}

/** A server error, assignable to `@dynamicforms/vue-forms`' `ErrorDescription` so it renders like validator errors. */
export interface ErrorDescription {
  readonly code: string;
  readonly params: Readonly<Record<string, unknown>>;
  readonly detail: string;
  readonly origin: 'server';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Whether `data` has the shape of an `ApiErrorBody`. */
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
 * The `ApiErrorBody` in `error.response.data` (axios or `ViewSetRequestError`). Otherwise builds
 * `fastapi-viewsets.no_response` when there is no response, or `fastapi-viewsets.unexpected_response`
 * (`{ status }`) when the body is not an `ApiErrorBody`.
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

/** The `ErrorDescription` of an `ApiErrorBody`. */
export function toErrorDescription(body: ApiErrorBody): ErrorDescription {
  return {
    code: body.detail_code,
    params: body.detail_params,
    detail: body.detail,
    origin: 'server',
  };
}

/** `field_errors` as `ErrorDescription`s keyed by dotted field name (`address.city`; `''` for the whole body). */
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
 * What a failed muxws call throws. Mirrors `AxiosError`'s `response.status/data/headers`, so callers
 * read errors the same on both transports; `response` is always set.
 */
export class ViewSetRequestError extends Error {
  readonly response: { status: number; data: unknown; headers: Record<string, string> };

  constructor(status: number, data: unknown, headers: Record<string, string> = {}) {
    super(`Request failed with status code ${status}`);
    this.name = 'ViewSetRequestError';
    this.response = { status, data, headers };
  }
}
