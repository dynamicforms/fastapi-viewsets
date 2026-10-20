import { ERROR_NAMESPACE_PYDANTIC } from './error-namespace';

/**
 * The body of a failed request that names one failure. `detail` is plain English text.
 * `detail_code` and `detail_params` appear when the server has registered
 * `df_viewset_exception_handler` (see the Python side's `fastapi_viewsets.exceptions`) and the
 * error is a `DfViewSetError`; a view's own `raise HTTPException(status_code, detail="...")` carries
 * `detail` alone. `detail_code` is a full code, `<namespace>.<code>`: `fastapi-viewsets.<code>` for
 * the errors the package raises, the application's own code as given for its own errors.
 */
export interface ApiErrorBody {
  detail: string;
  detail_code?: string;
  detail_params?: Record<string, unknown>;
}

/**
 * One failure of a request body's validation, as FastAPI reports it with status 422. `loc` is the
 * path to the failing value, starting with where it was read from (`body`, `query`, `path`,
 * `header`, `cookie`); `type` is pydantic's code for the failure and `ctx` the values its `msg` was
 * built from.
 */
export interface FieldErrorEntry {
  type: string;
  loc: (string | number)[];
  msg: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

/** The body of a 422 response to a request whose parameters or body failed validation. */
export interface FieldErrorsBody {
  detail: FieldErrorEntry[];
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

/**
 * The error a failed request's body describes: `detail_code` as `code`, unchanged (an empty string
 * where the body has none), `detail_params` as `params` and `detail` unchanged.
 */
export function toErrorDescription(body: ApiErrorBody): ErrorDescription {
  return {
    code: body.detail_code ?? '',
    params: body.detail_params ?? {},
    detail: body.detail,
    origin: 'server',
  };
}

/**
 * The errors of a 422 body, keyed by the name of the failing field: `loc` without its first
 * element, joined with `.` (`['body', 'address', 'city']` is `address.city`). A failure of the body
 * as a whole is keyed `''`. Each error has pydantic's `type` in the namespace
 * `ERROR_NAMESPACE_PYDANTIC` as `code` (`pydantic.string_too_short`), `ctx` as `params` and `msg`
 * as `detail`.
 */
export function toFieldErrors(body: FieldErrorsBody): Record<string, ErrorDescription[]> {
  const errors: Record<string, ErrorDescription[]> = {};
  for (const entry of body.detail) {
    const field = entry.loc.slice(1).join('.');
    (errors[field] ??= []).push({
      code: `${ERROR_NAMESPACE_PYDANTIC}.${entry.type}`,
      params: entry.ctx ?? {},
      detail: entry.msg,
      origin: 'server',
    });
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
