/**
 * The body of a failed request that names one failure. `detail` is plain English text.
 * `detail_code` and `detail_params` appear when the server has registered
 * `df_viewset_exception_handler` (see the Python side's `fastapi_viewsets.exceptions`) and the
 * error is a `DfViewSetError`; a view's own `raise HTTPException(status_code, detail="...")` carries
 * `detail` alone.
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
 * The error a failed request's body describes: `detail_code` as `code` (an empty string where the
 * body has none), `detail_params` as `params` and `detail` unchanged.
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
 * as a whole is keyed `''`. Each error has pydantic's `type` as `code`, `ctx` as `params` and `msg`
 * as `detail`.
 */
export function toFieldErrors(body: FieldErrorsBody): Record<string, ErrorDescription[]> {
  const errors: Record<string, ErrorDescription[]> = {};
  for (const entry of body.detail) {
    const field = entry.loc.slice(1).join('.');
    (errors[field] ??= []).push({ code: entry.type, params: entry.ctx ?? {}, detail: entry.msg, origin: 'server' });
  }
  return errors;
}
