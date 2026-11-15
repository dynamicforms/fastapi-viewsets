# Error Codes

Every error response of the package has one body: a stable `code`, the `params` its `detail` was built
from, `detail` itself and, for a request that failed validation, the failures per field. This page covers why that
exists, which errors carry which code, and how the frontend reads it.

## Design

`detail` is plain English text meant for a developer reading a response in a browser or a log, not
for showing to an end user in their own language. Translating it would mean either the server
picking a locale for a client it does not know, or an application parsing English sentences to
recover what went wrong - both wrong. A `code` names the failure independently of the wording, and
`params` are the values that wording was interpolated with, so a client can rebuild the message in
any language without ever reading `detail`.

`detail` is in English, except in field errors: the message of a pydantic custom error, `value_error` or
`assertion_error` contains the application's text, and the message of an error from Django's `ValidationError` is in
Django's active language. The client translates an error by its `code`.

### The error body

Every error of a viewset's route is answered with `ApiErrorBody` (`fastapi_viewsets.error_responses`), over REST and
over [muxws](./muxws) alike:

```json
{
  "detail": "Item with pk 42 not found",
  "detail_code": "fastapi-viewsets.not_found",
  "detail_params": { "pk": 42 },
  "field_errors": {}
}
```

| Field | Value |
|-------|-------|
| `detail` | English text, interpolated from `detail_params` |
| `detail_code` | the full code, `<namespace>.<code>` |
| `detail_params` | the values `detail` was built from; `{}` without any |
| `field_errors` | the failures of a request that failed validation, by field name (see [Field errors](#field-errors)); `{}` for every other error |

The body is built from the exception the request raised:

| Exception | Status | `detail_code` | `detail_params` |
|-----------|--------|---------------|-----------------|
| a `DfViewSetError` (the package's errors, an application's subclass) | its own | its `code` | its `params` |
| `RequestValidationError` (FastAPI's validation of the request, or raised by the endpoint) | 422 | `fastapi-viewsets.validation_failed` | `{}` |
| Django's or adorm's `ValidationError`, when installed (see [Django's ValidationError](./django-integration#django-s-validationerror) and [adorm's errors](./adorm-integration#errors)) | 422 | `fastapi-viewsets.validation_failed` | `{}` |
| any other `HTTPException`, FastAPI's or Starlette's | its own | `fastapi-viewsets.http_error` | `{status}` |
| any other exception | 500 | `fastapi-viewsets.unhandled_error` | `{}` |

An `HTTPException` keeps its `headers`. Its `detail` is the body's `detail` when it is a string; any other `detail`
is `detail_params.detail`, and the body's `detail` is the status phrase (`Bad Request`). The body of an unhandled
exception names no part of it: `detail` is `Internal Server Error`, and the exception is re-raised after the
response, so the server logs it as it logs any exception of a request. A status that allows no body (204, 304) is
answered without one.

`ApiErrorBody`, and `ApiFieldError` for one field error, are the named schemas of every error response in the
viewsets' OpenAPI document (see [Documenting endpoints](./endpoint-docs#error-responses)).

### The routes of a viewset

The routes `route_viewset` adds answer every error of their endpoint, its dependencies and its request validation
with the body, without any registration by the application:

- the package's handler of `HTTPException` and of `RequestValidationError` replaces FastAPI's default handlers for
  the request, and also handles Django's and adorm's `ValidationError` when they are installed;
- an exception without a handler is answered with 500 `fastapi-viewsets.unhandled_error` and re-raised.

A handler the application registered itself takes precedence, as Starlette's lookup gives it: a handler for a class
of exception (`app.add_exception_handler(MyDomainError, ...)`), for a status, for `HTTPException`, and for
`Exception` or 500 answer as the application wrote them. A dependency with `yield` sees the exception before the
response is built.

### The other routes: `install_error_handlers`

A route of the application outside the viewsets, and a request no route matches, are answered by FastAPI's own
handlers: `{"detail": "..."}`, a 422 with pydantic's list of errors, plain-text `Internal Server Error`.
`install_error_handlers(app)` registers handlers that answer them with the body too:

```python
from fastapi_viewsets.error_responses import install_error_handlers

app = FastAPI()
install_error_handlers(app)
```

It registers one handler for `DfViewSetError`, `HTTPException`, `RequestValidationError`, Django's and adorm's
`ValidationError` (when installed) and `Exception`. Starlette's
routing errors are not the errors of a viewset: they are answered with the body only once the handlers are
installed, as `fastapi-viewsets.http_error` with `detail` Starlette's text and the status in `detail_params`:

| Request | Status | Body |
|---------|--------|------|
| a path no route matches | 404 | `{"detail": "Not Found", "detail_code": "fastapi-viewsets.http_error", "detail_params": {"status": 404}, "field_errors": {}}` |
| a method the route of the path does not serve | 405 | `{"detail": "Method Not Allowed", "detail_code": "fastapi-viewsets.http_error", "detail_params": {"status": 405}, "field_errors": {}}`, with `Allow` |

A viewset's `NotFoundError` (`fastapi-viewsets.not_found`, an item with no such `pk`) is an error of the viewset's
route and carries its own code either way. A handler the application registers after `install_error_handlers`
replaces the installed one for its class.

### Namespaces

A code is a full code, `<namespace>.<code>`:

- `code` is snake_case and contains no `.`: `[a-z0-9]+(_[a-z0-9]+)*`;
- `namespace` is one or more segments joined by `.`, each `[a-z0-9]+(-[a-z0-9]+)*`;
- a full code splits at its last `.`: everything before it is the namespace, everything after it the code.

The namespace names the package that declared the code. The package exports its namespaces from its root, with the
same values in Python and TypeScript:

| Constant | Value | Codes |
|----------|-------|-------|
| `ERROR_NAMESPACE` | `fastapi-viewsets` | the codes this package declares, e.g. `fastapi-viewsets.not_found` |
| `ERROR_NAMESPACE_PYDANTIC` | `pydantic` | pydantic's error types in field errors that do not become a [field error code](#field-error-codes), e.g. `pydantic.json_invalid`, and `pydantic.custom_error` |
| `ERROR_NAMESPACE_DJANGO` | `django` | Django's validation codes in field errors that do not become a [field error code](#field-error-codes), e.g. `django.unique_for_date` |

```python
from fastapi_viewsets import ERROR_NAMESPACE, ERROR_NAMESPACE_DJANGO, ERROR_NAMESPACE_PYDANTIC
```

```ts
import { ERROR_NAMESPACE, ERROR_NAMESPACE_DJANGO, ERROR_NAMESPACE_PYDANTIC } from '@dynamicforms/fastapi-viewsets';
```

The code of an application's own `DfViewSetError` is used as given: the package does not prefix, check or rewrite it.
The application prefixes its own codes with a namespace of its own, such as `app.insufficient_balance`, so that they
do not collide with the codes of a library.

The package ships no translation keys. The application maps each namespace to a translation key prefix of its own;
see [Frontend](#translating-the-codes).

## Backend

### Built-in errors

| Class | Status | `code` | `params` |
|-------|--------|--------|----------|
| `NotFoundError(pk)` | 404 | `fastapi-viewsets.not_found` | `{pk}` |
| `ReadOnlyCollectionError()` | 405 | `fastapi-viewsets.read_only` | — |
| `SessionExpiredError()` | 401 | `fastapi-viewsets.session_expired` | — |
| `NotAuthorizedError()` | 403 | `fastapi-viewsets.not_authorized` | — |
| `RateLimitedError()` | 429 | `fastapi-viewsets.rate_limited` | — |
| `IdempotencyKeyInvalidError()` | 400 | `fastapi-viewsets.idempotency_key_invalid` | — |
| `IdempotencyKeyReusedError(key)` | 422 | `fastapi-viewsets.idempotency_key_reused` | `{key}` |
| `ResultTimeoutError(task, timeout)` | 504 | `fastapi-viewsets.result_timeout` | `{task, timeout}` |
| `ResultReaderStoppedError(queue_key)` | 503 | `fastapi-viewsets.result_reader_stopped` | `{queue_key}` |
| `ActionDispatchError(task, reason=None)` | 503 | `fastapi-viewsets.action_dispatch_failed` | `{task, reason}` |
| `ActionInterruptedError(task, reason)` | 504 for `time_limit`, else 503 | `fastapi-viewsets.action_interrupted` | `{task, reason}` |
| `ActionFailedError(task, reason="error")` | 500 | `fastapi-viewsets.action_failed` | `{task, reason}` |
| `UnsupportedListShapeError(shape, allowed)` | 422 | `fastapi-viewsets.unsupported_list_shape` | `{shape, allowed}` |
| `EventTopicInvalidError(topic)` | 422 | `fastapi-viewsets.event_topic_invalid` | `{topic}` |
| `EventTopicRefusedError(topic)` | 403 | `fastapi-viewsets.event_topic_refused` | `{topic}` |
| `EventScopeMissingError(topic)` | 403 | `fastapi-viewsets.event_scope_missing` | `{topic}` |
| `EventSubscriptionsExceededError(limit)` | 409 | `fastapi-viewsets.event_subscriptions_exceeded` | `{limit}` |
| `EventCursorsExceededError(count, limit)` | 422 | `fastapi-viewsets.event_cursors_exceeded` | `{count, limit}` |
| `CursorRequestError(error)` | 400 | one of the cursor codes below | varies |
| `UnhandledError()` | 500 | `fastapi-viewsets.unhandled_error` | — |
| `CommandInvalidError(reason)` | 400 | `fastapi-viewsets.command_invalid` | `{reason}` |

`UnhandledError` is the error an exception without a handler is answered with, and the error a stored outcome of one
is rebuilt as (see [Custom errors](#custom-errors)). `CommandInvalidError` answers a muxws stream addressed to a
viewset whose headers no route can be matched against; `reason` is `address_missing` (no `:method` or `:path`),
`address_type` (`:method` or `:path` is not a string), `path_relative` (`:path` does not start with `/`) or
`query_type` (`:query` is neither a string nor a mapping).

The codes the package answers errors with that no exception class of its own carries:

| `code` | Status | `params` | When |
|--------|--------|----------|------|
| `fastapi-viewsets.validation_failed` | 422 | — | a `RequestValidationError`, or Django's or adorm's `ValidationError` when installed; the failures are in `field_errors` |
| `fastapi-viewsets.http_error` | the exception's | `{status}`, and `{detail}` for a `detail` that is not a string | an `HTTPException` that is not a `DfViewSetError` |

The cursor codes, raised as `fastapi_viewsets.cursor.CursorError` (a `ValueError` carrying `code`
and `params`) when cursor pagination rejects a request, and surfaced through `CursorRequestError`:

| `code` | `params` | When |
|--------|----------|------|
| `fastapi-viewsets.cursor_unreadable` | `{error}` | the cursor string does not decode |
| `fastapi-viewsets.cursor_missing_position` | — | it decodes but carries no position |
| `fastapi-viewsets.cursor_stale` | — | it was issued for a different ordering or filter |
| `fastapi-viewsets.cursor_missing_keys` | `{missing}` | it has no value for one or more ordering keys |
| `fastapi-viewsets.cursor_value_mismatch` | `{name, error}` | a value in it does not fit the field's type |

```python
from fastapi_viewsets.exceptions import NotFoundError

raise NotFoundError(pk)
# 404 {"detail": "Item with pk 42 not found", "detail_code": "fastapi-viewsets.not_found", "detail_params": {"pk": 42}, "field_errors": {}}
```

### Executor outcomes

The errors that end a call to a `celery_viewset` action without its result, by `reason` (see
[Outcomes of a call](./routers#outcomes-of-a-call)):

| `code` | `reason` | The action |
|--------|----------|------------|
| `fastapi-viewsets.action_dispatch_failed` | `send` | was not sent: the broker is unreachable, or the message cannot be encoded |
| | `serialization` | was not sent: its arguments, context or request cannot be serialized (e.g. a `BackgroundTasks` argument, see [Arguments and results](./routers#arguments-and-results)) |
| | `unknown_task` | did not run: the worker has no task of that name |
| | `rejected` | did not run: the worker rejected the message |
| | `revoked`, `expired` | did not run: it was revoked, or expired, before it started |
| `fastapi-viewsets.action_interrupted` | `time_limit` | exceeded its soft or hard time limit (504) |
| | `terminated` | was terminated (503) |
| | `lost` | lost the worker process executing it (503) |
| `fastapi-viewsets.action_failed` | `error` | raised an error that is not an `HTTPException`, a `RequestValidationError` or Django's or adorm's `ValidationError`; `detail` names the task only, the error is logged in the worker |
| | `result` | returned a result that cannot be serialized (e.g. a `StreamingResponse`), or that the caller cannot deserialize |
| | `executor` | did not complete because the worker failed around it, or `Middleware.executor_started()` raised |

`ResultTimeoutError` and `ResultReaderStoppedError` descend from `ActionOutcomeUnknownError`: the
caller stopped waiting, and the action may have run, may still run, or never will.

### Custom errors

Subclass `DfViewSetError` for an application's own errors that should carry the same shape. The code is the
application's, in a namespace of its own:

```python
from fastapi_viewsets.exceptions import DfViewSetError

class InsufficientBalanceError(DfViewSetError):
    def __init__(self, required: float, available: float):
        message = f"balance {available} is short of the required {required}"
        super().__init__(402, message, "app.insufficient_balance", {"required": required, "available": available})
```

A route of a viewset answers every subclass with its `code` and `params`, the package's own errors and an
application's own alike; `install_error_handlers` does the same for the application's other routes.

`error_entry()` (`fastapi_viewsets.response_classes`) documents such errors as an OpenAPI `responses` entry whose body
is `ApiErrorBody`, with one example per exception, built as the error is answered; `merge_responses()` merges entries
of the same status. See [Documenting endpoints](./endpoint-docs#error-responses):

```python
from fastapi_viewsets.response_classes import error_entry

PAYMENT_REQUIRED = error_entry(
    402, "The balance does not cover the call: `app.insufficient_balance`.", InsufficientBalanceError(10, 2.5)
)
# {"402": {"model": ApiErrorBody, "description": "...", "content": {"application/json": {"examples": {
#     "insufficient_balance": {"summary": "balance 2.5 is short of the required 10", "value": {...}}}}}}}
```

An application's own subclass raised by a `celery_viewset` action reaches the FastAPI process as a
`DfViewSetError` with the same status, `detail`, `code`, `params` and headers; only the classes
`fastapi_viewsets.exceptions` defines are rebuilt as their own class.

The error travels from the worker as JSON fields, in the result payload of the call, and an
[idempotency](./idempotency) record stores it the same way; a pickled `DfViewSetError` keeps its fields too. The
fields hold the full code, `fastapi-viewsets.not_found` or the application's own, and the reader reads it as given.
They hold the error, not the response body; the body is built when the response is, by the rules of
[The error body](#the-error-body), so a replayed error and a result read from a worker are answered as the error was
when it was raised:

| Error | Stored fields | Rebuilt as |
|-------|---------------|------------|
| a `DfViewSetError` | `status_code`, `detail`, `headers`, `class`, `code`, `params` | the class `class` names where `fastapi_viewsets.exceptions` defines it, else `DfViewSetError` |
| another `HTTPException` | `status_code`, `detail`, `headers` | `HTTPException` |
| `RequestValidationError`, or Django's or adorm's `ValidationError` converted to a `RequestValidationError` of its field errors | `status_code` 422, pydantic's errors as `detail`, `headers` null, `class` `"RequestValidationError"` | `RequestValidationError` |
| any other exception (an idempotency record; a `celery_viewset` worker answers it as `ActionFailedError`) | the fields of `UnhandledError` | `UnhandledError` |

## Frontend

`@dynamicforms/fastapi-viewsets` reads errors as `ApiErrorBody`, and converts one into an `ErrorDescription`: `code`,
`params`, the `detail` and `origin: 'server'`. The interface has the shape of
`ErrorDescription` in `@dynamicforms/vue-forms`, so the application renders and translates a
server's errors with the same function as its validators' errors. The package holds no
translations. How an application converts an error into text in its own language is described in
[vue-forms: Error messages and translation](:vue-forms:/api/validators.html#error-messages-and-translation);
the codes of this page are added to the application's translations under the prefixes of their namespaces (see
[Translating the codes](#translating-the-codes)).

### The body of a failed call

A failed call throws an `AxiosError` over REST and a `ViewSetRequestError` over muxws; both carry the response as
`error.response`, with the body in `error.response.data`. `errorBodyOf(error)` returns that body when it is an
`ApiErrorBody` (`isApiErrorBody(data)`). A call the BE did not answer has no body, and `errorBodyOf` builds one; this
is the only place the client describes an error itself:

| The call | `detail_code` | `detail_params` |
|----------|---------------|-----------------|
| got no response: a network failure, a timeout, a cancelled request, a muxws stream reset or a lost connection, any error without `response` | `fastapi-viewsets.no_response` | — |
| got a response whose body is not an `ApiErrorBody`: a proxy's HTML 502, a route of the application without `install_error_handlers` | `fastapi-viewsets.unexpected_response` | `{status}`, null where the response has none |

```ts
import { errorBodyOf, toErrorDescription } from '@dynamicforms/fastapi-viewsets';

try {
  await itemsApi.retrieve(42);
} catch (error) {
  const description = toErrorDescription(errorBodyOf(error));
  // { code: 'fastapi-viewsets.not_found', params: { pk: 42 }, detail: 'Item with pk 42 not found', origin: 'server' }
}
```

### One error

`toErrorDescription(body)` converts an `ApiErrorBody`:

| Field | Value |
|-------|-------|
| `code` | `detail_code`, unchanged |
| `params` | `detail_params`, as the server sent them |
| `detail` | `detail`, unchanged |
| `origin` | `'server'` |

### Field errors

A request whose body or parameters fail validation is answered with status 422,
`fastapi-viewsets.validation_failed`, and its failures in `field_errors`, over REST and muxws alike:

```json
{
  "detail": "The request failed validation",
  "detail_code": "fastapi-viewsets.validation_failed",
  "detail_params": {},
  "field_errors": {
    "name": [{ "code": "fastapi-viewsets.min_length", "params": { "minLength": 3 }, "detail": "String should have at least 3 characters" }],
    "address.zip": [{ "code": "fastapi-viewsets.invalid_integer", "params": {}, "detail": "Input should be a valid integer" }]
  }
}
```

`field_errors` is built from pydantic's errors and from Django's and adorm's `ValidationError` (see [Django's
ValidationError](./django-integration#django-s-validationerror)); an error of adorm's `full_clean()` carries no code
and answers `fastapi-viewsets.invalid` with adorm's message in `detail`. The name of a
field is pydantic's `loc` without its first element (`body`, `query`, `path`, `header` or `cookie`), joined with `.`; a
failure of the body as a whole is keyed `''`. Each entry (`ApiFieldError`) has `code`, `params` and `detail`. `detail`
is pydantic's `msg` or Django's message. pydantic's messages are English; the message of a `PydanticCustomError`, a
`value_error` or an `assertion_error` contains the application's text; Django's message is in Django's active language.
pydantic's `input` is left out.

#### Field names

On a viewset's endpoint, the viewset's `error_field(context, field, code)` returns the key of each field error before
it goes into `field_errors`. It is called for every field error: the request's validation (body, query, path), the
pydantic errors of a backend's record and Django's `ValidationError`. `field` is the key described above (`''` for the
body as a whole or a non-field error). `code` is the error's code before the package's field error codes apply:
`pydantic.<type>` for a pydantic type (`pydantic.string_too_short`), `django.<code>` for a Django code (`django.unique`;
`django.invalid` for an error without a code), a namespaced type or code as it is (`app.slug_taken`), and
`pydantic.custom_error` for a custom type without a namespace. The default returns `field`. A result of `''` puts the
error on the form.

```python
from fastapi_viewsets.backends.django_orm import DjangoORMViewSet
from fastapi_viewsets.context import Context
from fastapi_viewsets.mixins import BulkViewSetMixin


class TrackViewSet(DjangoORMViewSet[int, Track], BulkViewSetMixin[int, Track]):
    model = TrackModel
    schema = Track

    def error_field(self, context: Context, field: str, code: str) -> str:
        if field == "slug":  # a model field that is not in the schema
            return "title"
        if code == "django.unique":
            return ""
        return field
```

- In `bulk_create`, `bulk_update`, `bulk_partial_update` and `bulk_destroy`, the key of a body error starts with the
  row's index or primary key (`3.title`). `field` is the part after it (`title`, or `''` for the row itself), and the
  row's key is put in front of the result. A result of `''` puts the error on the row (`3`). A `RequestValidationError`
  raised by the application in these actions has the row's key as the first `loc` element after `body`.
- A Django `ValidationError` raised in these actions by a `perform_*` method or an action has no row's key: `field` is
  the Django field name, and the result is the key.
- In `bulk_destroy`, the field errors in the `ApiErrorBody` of one key have no row key.
- `context` has the request's context values; `context.action_name` is the action. `error_field` is synchronous, and
  the context's values are awaitables. The request's validation fails before the context processors run when the body
  is not valid JSON; the context then has no values, and no context processor runs for `error_field`.
- The viewset that routes the endpoint names its errors: on a sub-viewset's endpoint, the sub-viewset.
- Every package mixin and backend has `error_field`. A viewset class without them can define it.
- The field errors of a `celery_viewset` action and of an idempotent repeat are named in the FastAPI process. The
  worker's payload and the stored outcome keep the codes `error_field` receives.
- An exception raised in `error_field` answers 500 `fastapi-viewsets.unhandled_error`.
- A route outside the viewsets, under `install_error_handlers`, keeps the keys. A `RequestValidationError` handler
  registered by the application answers without `error_field`.

#### Field error codes

The package declares the field error codes below, in its namespace. Each code has a fixed set of params with camelCase
names. Where `@dynamicforms/vue-forms` has a validator code with the same meaning, the code has the same name. The
codes and their params are listed in `FIELD_ERROR_PARAMS` (`fastapi_viewsets`) and in `FieldErrorParams` and
`FIELD_ERROR_PARAM_NAMES` (TS package):

| `code` | Params | Meaning |
|--------|--------|---------|
| `fastapi-viewsets.required` | — | no value given, or null where null is not allowed |
| `fastapi-viewsets.min_length` | `minLength` | too few characters, items or bytes |
| `fastapi-viewsets.max_length` | `maxLength` | too many characters, items or bytes |
| `fastapi-viewsets.min_value` | `minValue` | below an inclusive lower bound |
| `fastapi-viewsets.min_value_exclusive` | `minValue` | not greater than an exclusive lower bound |
| `fastapi-viewsets.max_value` | `maxValue` | above an inclusive upper bound |
| `fastapi-viewsets.max_value_exclusive` | `maxValue` | not less than an exclusive upper bound |
| `fastapi-viewsets.multiple_of` | `multipleOf` | not a multiple of `multipleOf` |
| `fastapi-viewsets.max_digits` | `maxDigits` | too many digits in total |
| `fastapi-viewsets.max_decimal_places` | `maxDecimalPlaces` | too many decimal places |
| `fastapi-viewsets.max_whole_digits` | `maxWholeDigits` | too many digits before the decimal point |
| `fastapi-viewsets.pattern` | `pattern` | does not match the regular expression; `pattern` is its source |
| `fastapi-viewsets.in_allowed_values` | `allowedAsText` | not one of the allowed values; `allowedAsText` is pydantic's text of them, e.g. `'a', 'b' or 'c'` |
| `fastapi-viewsets.invalid_choice` | — | not an allowed value; the allowed values are not in `params` |
| `fastapi-viewsets.invalid_integer` | — | not an integer |
| `fastapi-viewsets.invalid_number` | — | not a number |
| `fastapi-viewsets.invalid_boolean` | — | not a boolean |
| `fastapi-viewsets.invalid_date` | — | not a date |
| `fastapi-viewsets.invalid_time` | — | not a time |
| `fastapi-viewsets.invalid_datetime` | — | not a date and time |
| `fastapi-viewsets.invalid_duration` | — | not a duration |
| `fastapi-viewsets.invalid_url` | — | not a URL, or a URL with invalid syntax |
| `fastapi-viewsets.invalid_uuid` | — | not a UUID |
| `fastapi-viewsets.past` | — | a date or date and time not in the past |
| `fastapi-viewsets.future` | — | a date or date and time not in the future |
| `fastapi-viewsets.invalid_type` | — | the value has the wrong JSON or structural type |
| `fastapi-viewsets.unique` | `fields` | a row with the same values in `fields` exists; `fields` are Django model field names; from `AdormViewSet` the names clients know the adorm model's fields by (an alias where the field has one) |
| `fastapi-viewsets.invalid` | — | not valid; the reason is in `detail` |
| `fastapi-viewsets.pk_assigned_by_server` | — | a primary key in the body of a create |
| `fastapi-viewsets.protected` | — | a delete refused because other records refer to the record through a `PROTECT` reference |
| `fastapi-viewsets.restricted` | — | a delete refused because other records refer to the record through a `RESTRICT` reference |

`minLength`, `maxLength`, `multipleOf`, `maxDigits`, `maxDecimalPlaces` and `maxWholeDigits` are numbers. `minValue`
and `maxValue` are numbers, or ISO strings for a date or time bound. `fields` is an array of strings.
`allowedAsText` has a different format from vue-forms' `allowedAsText` (`a, b, c`). It is pydantic's `expected`
text; pydantic's error does not contain the allowed values as a list.

pydantic error types become these codes as follows. Keys of pydantic's `ctx` that the table does not list are left
out:

| `code` | pydantic types | Params from `ctx` |
|--------|----------------|-------------------|
| `required` | `missing`, `missing_argument`, `missing_keyword_only_argument`, `missing_positional_only_argument` | — |
| `min_length` | `string_too_short`, `too_short`, `bytes_too_short` | `min_length` |
| `max_length` | `string_too_long`, `too_long`, `bytes_too_long`, `url_too_long` | `max_length` |
| `min_value` / `min_value_exclusive` | `greater_than_equal` / `greater_than` | `ge` / `gt` |
| `max_value` / `max_value_exclusive` | `less_than_equal` / `less_than` | `le` / `lt` |
| `multiple_of` | `multiple_of` | `multiple_of` |
| `max_digits`, `max_decimal_places`, `max_whole_digits` | `decimal_max_digits`, `decimal_max_places`, `decimal_whole_digits` | `max_digits`, `decimal_places`, `whole_digits` |
| `pattern` | `string_pattern_mismatch` | `pattern` |
| `in_allowed_values` | `enum`, `literal_error` | `expected` |
| `invalid_integer` | `int_type`, `int_parsing`, `int_parsing_size`, `int_from_float` | — |
| `invalid_number` | `float_type`, `float_parsing`, `finite_number`, `decimal_type`, `decimal_parsing`, `fraction_type`, `fraction_parsing` | — |
| `invalid_boolean` | `bool_type`, `bool_parsing` | — |
| `invalid_date` | `date_type`, `date_parsing`, `date_from_datetime_parsing`, `date_from_datetime_inexact` | — |
| `invalid_time` | `time_type`, `time_parsing` | — |
| `invalid_datetime` | `datetime_type`, `datetime_parsing`, `datetime_object_invalid`, `datetime_from_date_parsing` | — |
| `invalid_duration` | `time_delta_type`, `time_delta_parsing` | — |
| `invalid_url` | `url_type`, `url_parsing`, `url_syntax_violation` | — |
| `invalid_uuid` | `uuid_type`, `uuid_parsing` | — |
| `past` / `future` | `date_past`, `datetime_past` / `date_future`, `datetime_future` | — |
| `invalid_type` | `string_type`, `string_unicode`, `bytes_type`, `dict_type`, `frozen_dict_type`, `ordered_dict_type`, `counter_type`, `list_type`, `tuple_type`, `set_type`, `frozen_set_type`, `deque_type`, `mapping_type`, `iterable_type`, `model_type`, `model_attributes_type`, `dataclass_type`, `dataclass_exact_type`, `named_tuple_type`, `none_required`, `arguments_type`, `callable_type`, `is_instance_of`, `is_subclass_of` | — |
| `invalid` | `value_error`, `assertion_error` | — (the application's text is in `detail`) |

- An error of a type in the `invalid_*`, `invalid_type` or `in_allowed_values` rows becomes `fastapi-viewsets.required`
  when its input is null. A `value_error` with a null input stays `fastapi-viewsets.invalid`, with the validator's
  message in `detail`.
- An error of a type in the table becomes `pydantic.<type>`, with `ctx` as `params`, when its `ctx` does not contain a
  key listed in the 'Params from `ctx`' column.
- Any other pydantic type (the values of `pydantic_core.core_schema.ErrorType`, `PYDANTIC_ERROR_TYPES` in
  `fastapi_viewsets.error_responses`) becomes `pydantic.<type>`, with pydantic's `ctx` as `params`. An exception in
  `ctx` becomes its message. Examples: `pydantic.json_invalid`, `pydantic.extra_forbidden`, `pydantic.url_scheme`,
  `pydantic.timezone_naive`.
- The code of a `PydanticCustomError` whose type has a namespace, such as `app.slug_taken`, is that type; `params` is
  the error's `ctx`. An application that raises a custom type in the namespace `fastapi-viewsets` must give it a `ctx`
  with the params that code declares. The package does not check the params.
- A custom type without a namespace becomes `pydantic.custom_error` (`CUSTOM_ERROR_CODE`) with `params` `{type, ctx}`:
  the custom type and the error's `ctx`. `ctx` is nested so that its keys cannot collide with `type`.

```json
{
  "slug": [{ "code": "app.slug_taken", "params": { "slug": "blue" }, "detail": "Slug blue is taken" }],
  "id": [{ "code": "fastapi-viewsets.pk_assigned_by_server", "params": {}, "detail": "The primary key is assigned by the server" }]
}
```

The TS package declares types for the codes and their params. `isFieldErrorCode(code)` returns whether `code` is one
of the package's field error codes. `FieldErrorOf<C>` is the type of a field error with code `C`:

```ts
import { type ApiFieldError, type FieldErrorOf, isFieldErrorCode } from '@dynamicforms/fastapi-viewsets';

function lengthHint(error: ApiFieldError): string {
  if (error.code === 'fastapi-viewsets.min_length') {
    const { minLength } = (error as FieldErrorOf<'fastapi-viewsets.min_length'>).params;
    return `at least ${minLength}`;
  }
  return isFieldErrorCode(error.code) ? '' : error.detail;
}
```

`toFieldErrors(body)` converts `field_errors` into `ErrorDescription`s keyed by the same names; it is `{}` for a body
without field errors:

```ts
import { toFieldErrors } from '@dynamicforms/fastapi-viewsets';

toFieldErrors(body);
// {
//   name: [{ code: 'fastapi-viewsets.min_length', params: { minLength: 3 }, detail: 'String should have at least 3 characters', origin: 'server' }],
//   'address.zip': [{ code: 'fastapi-viewsets.invalid_integer', params: {}, detail: 'Input should be a valid integer', origin: 'server' }],
// }
```

A name is a path through the members of a vue-forms form: a `Group` member by its name, a `List`
row by its index. vue-forms' [`elementAt()`](:vue-forms:/api/container.html#elementat-root-path-fieldbase-undefined)
returns the element at a name, the form itself for `''`, and `undefined` for a name the form does not hold; the errors
of such a name belong to the form itself. A `SubmitAction` sends the form through a ViewSet and, where the server
refuses it, writes the server's errors to the form before it throws. The example needs `@dynamicforms/vue-forms`
3.0.0-alpha.4 or later:

```ts
import {
  type ErrorDescription,
  errorBodyOf,
  toErrorDescription,
  toFieldErrors,
} from '@dynamicforms/fastapi-viewsets';
import {
  Action,
  elementAt,
  type FieldBase,
  Group,
  SubmitAction,
  ValidationError,
} from '@dynamicforms/vue-forms';

function addErrors(element: FieldBase, errors: ErrorDescription[]) {
  element.errors = [
    ...element.errors,
    ...errors.map((e) => new ValidationError(e.code, e.params, e.detail, e.origin)),
  ];
}

const save = new Action({
  value: { label: 'Save', defaultConfirm: true },
  actions: [
    new SubmitAction(
      (action) => action.parent?.parent,
      async (value, target) => {
        try {
          return await itemsApi.update(value.id, value);
        } catch (error) {
          const body = errorBodyOf(error);
          const fields = Object.entries(toFieldErrors(body));
          for (const [name, errors] of fields) addErrors(elementAt(target, name) ?? target, errors);
          if (fields.length === 0) addErrors(target, [toErrorDescription(body)]);
          throw error;
        }
      },
    ),
  ],
});

const form = new Group({ /* the item's fields */ actions: new Group({ save }) });
```

`execute()` of the action, and `form.confirm()`, resolve with a `SubmitFailedException` whose
`cause` is the error; see vue-forms' [`SubmitAction`](:vue-forms:/api/action.html#submitaction-target-handler-options).
Its button in `<df-actions>` of `@dynamicforms/vuetify-inputs` is disabled while the form is
invalid, so the form is not sent again until the user has changed a value the server refused.

Whether an error of origin `'server'` counts toward `valid` and when it is removed is set by
vue-forms' [`ErrorPolicy`](:vue-forms:/api/validators.html#errorpolicy). By default it counts, and
an element drops it when the element, or a member below it, is marked touched while the element
holds a value other than the one it held when the error was written.

The codes are:

- the [field error codes](#field-error-codes), with their params;
- `pydantic.<type>`, with pydantic's `ctx` as params;
- `django.<code>`, with Django's params (JSON values only);
- the application's namespaced codes, with the custom error's `ctx` or Django's params.

The `params` of `pydantic.custom_error` are `{type, ctx}`; `type` is the application's custom type. A translation of
`pydantic.custom_error` can select its text by `params.type`.

### Codes

The codes this package raises, with their `detail`:

| `code` | `detail` | Params |
|--------|----------|--------|
| `fastapi-viewsets.not_found` | `Item with pk {pk} not found` | `pk` |
| `fastapi-viewsets.read_only` | `This collection is read-only` | — |
| `fastapi-viewsets.session_expired` | `Session expired or invalid` | — |
| `fastapi-viewsets.not_authorized` | `Not authorized to perform this action` | — |
| `fastapi-viewsets.rate_limited` | `Rate limit exceeded` | — |
| `fastapi-viewsets.result_timeout` | `No result for {task} within {timeout} s` | `task`, `timeout` |
| `fastapi-viewsets.result_reader_stopped` | `The result reader for {queue_key} stopped before the result arrived` | `queue_key` |
| `fastapi-viewsets.action_dispatch_failed` | `{task} could not be handed to its executor` | `task`, `reason` |
| `fastapi-viewsets.action_interrupted` | `{task} exceeded its time limit` (`time_limit`), `{task} was terminated` (`terminated`), `{task} lost the process executing it` (`lost`) | `task`, `reason` |
| `fastapi-viewsets.action_failed` | `{task} failed`; `The action failed` without a task | `task`, `reason` |
| `fastapi-viewsets.idempotency_key_invalid` | `Idempotency-Key must be a non-empty Structured Field string` | — |
| `fastapi-viewsets.idempotency_key_reused` | `Idempotency key {key} was used for a different request` | `key` |
| `fastapi-viewsets.unsupported_list_shape` | `unsupported list shape '{shape}'; this endpoint offers {allowed}` | `shape`, `allowed` |
| `fastapi-viewsets.event_topic_invalid` | `Topic {topic} is neither a path starting with / nor a source name with an optional key` | `topic` |
| `fastapi-viewsets.event_topic_refused` | `Topic {topic} is not admitted` | `topic` |
| `fastapi-viewsets.event_scope_missing` | `Topic {topic} has no event scope for this caller` | `topic` |
| `fastapi-viewsets.event_subscriptions_exceeded` | `This socket already holds {limit} topics` | `limit` |
| `fastapi-viewsets.event_cursors_exceeded` | `A poll carries at most {limit} cursors, this one carries {count}` | `count`, `limit` |
| `fastapi-viewsets.cursor_unreadable` | `cursor is not readable: {error}` | `error` |
| `fastapi-viewsets.cursor_missing_position` | `cursor is not readable: no position in it` | — |
| `fastapi-viewsets.cursor_stale` | `this cursor was issued for a different ordering or filter - start from the first page` | — |
| `fastapi-viewsets.cursor_missing_keys` | `cursor has no value for ordering key(s): {missing}` | `missing` |
| `fastapi-viewsets.cursor_value_mismatch` | `cursor value for '{name}' does not fit the field: {error}` | `name`, `error` |
| `fastapi-viewsets.validation_failed` | `The request failed validation` | — |
| `fastapi-viewsets.pk_assigned_by_server` (a field error) | `The primary key is assigned by the server` | — |
| the other [field error codes](#field-error-codes) | pydantic's `msg` or Django's message (see [Field errors](#field-errors)) | see their table |
| `fastapi-viewsets.http_error` | the `HTTPException`'s `detail`; its status phrase for a `detail` that is not a string | `status`, `detail` |
| `fastapi-viewsets.unhandled_error` | `Internal Server Error` | — |
| `fastapi-viewsets.command_invalid` | `The stream headers carry no :method or :path` (`address_missing`), `:method and :path must both be strings` (`address_type`), `:path must be an absolute path` (`path_relative`), `:query must be a string or a mapping` (`query_type`) | `reason` |

The codes the client builds in `errorBodyOf()`:

| `code` | `detail` | Params |
|--------|----------|--------|
| `fastapi-viewsets.no_response` | `The server did not answer` | — |
| `fastapi-viewsets.unexpected_response` | `The server answered with status {status} and a body that is not an error body`; `unknown` for a `status` of null | `status` |

`allowed` and `missing` are arrays. An application's own `DfViewSetError` subclasses add their codes
to the translations under the prefix of their namespace.

### Translating the codes

The application maps each namespace to a translation key prefix of its own and the code after the namespace to a key
under that prefix, taking a library's namespace from its constant. Here `fastapi-viewsets.not_found` renders through
the key `libs.fastapi-viewsets.not_found`, `fastapi-viewsets.min_length` through `libs.fastapi-viewsets.min_length`,
`pydantic.json_invalid` through `libs.pydantic.json_invalid` and
the application's `app.insufficient_balance` through `errors.insufficient_balance`, with
[vue-i18n](https://vue-i18n.intlify.dev/)'s `t` and `te`:

```ts
import { ERROR_NAMESPACE, ERROR_NAMESPACE_PYDANTIC, type ErrorDescription } from '@dynamicforms/fastapi-viewsets';

const keyPrefixes: Record<string, string> = {
  [ERROR_NAMESPACE]: 'libs.fastapi-viewsets',
  [ERROR_NAMESPACE_PYDANTIC]: 'libs.pydantic',
  app: 'errors',
};

function errorText(error: ErrorDescription): string {
  const dot = error.code.lastIndexOf('.');
  const prefix = keyPrefixes[error.code.slice(0, dot)];
  const key = prefix === undefined ? undefined : `${prefix}.${error.code.slice(dot + 1)}`;
  return key !== undefined && te(key) ? t(key, error.params) : error.detail;
}
```

```json
{
  "libs": {
    "fastapi-viewsets": { "not_found": "Element {pk} ne obstaja", "min_length": "Vnesite vsaj {minLength} znake" },
    "pydantic": { "json_invalid": "Neveljaven JSON" }
  },
  "errors": { "insufficient_balance": "Stanje {available} ne zadošča za {required}" }
}
```

A `fastapi-viewsets.http_error` carries the `detail` of an `HTTPException` the application or a library raised
without a code; a translation keyed by `http_error` renders it by `params.status`. vue-forms' codes are mapped the
same way through vue-forms' own `ERROR_NAMESPACE`.
