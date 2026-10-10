# Error Codes

Every `HTTPException` this package raises on its own behalf carries a stable `code` and the
`params` its English `detail` was built from, alongside `detail` itself. This page covers why that
exists, how to opt into it on the backend, and how the frontend reads it.

## Design

`detail` is plain English text meant for a developer reading a response in a browser or a log, not
for showing to an end user in their own language. Translating it would mean either the server
picking a locale for a client it does not know, or an application parsing English sentences to
recover what went wrong - both wrong. A `code` names the failure independently of the wording, and
`params` are the values that wording was interpolated with, so a client can rebuild the message in
any language without ever reading `detail`.

`code` and `params` are additive, top-level siblings - `detail_code` and `detail_params` - never
nested inside `detail` itself: `detail` keeps its plain-string type for every consumer that already
reads it as text, and the two new fields appear only once the application opts in:

```python
from fastapi_viewsets.exceptions import DfViewSetError, df_viewset_exception_handler

app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)
```

An application that never registers this sees exactly what it always has, `{"detail": "..."}`,
from FastAPI's own default `HTTPException` handling. A view's own `raise HTTPException(status_code,
detail="...")` is unaffected either way - only exceptions descending from `DfViewSetError` carry a
`code`.

The server never picks a locale for a response: each request is handled independently, with no
per-request state that could hold one, so it always answers in English. Only the client, which
knows which user it is answering, decides what language to show.

## Backend

### Built-in errors

| Class | Status | `code` | `params` |
|-------|--------|--------|----------|
| `NotFoundError(pk)` | 404 | `not_found` | `{pk}` |
| `SessionExpiredError()` | 401 | `session_expired` | — |
| `NotAuthorizedError()` | 403 | `not_authorized` | — |
| `RateLimitedError()` | 429 | `rate_limited` | — |
| `UnsupportedListShapeError(shape, allowed)` | 422 | `unsupported_list_shape` | `{shape, allowed}` |
| `CursorRequestError(error)` | 400 | one of the cursor codes below | varies |

The cursor codes, raised internally when cursor pagination rejects a request and surfaced through
`CursorRequestError`:

| `code` | `params` | When |
|--------|----------|------|
| `cursor_unreadable` | `{error}` | the cursor string does not decode |
| `cursor_missing_position` | — | it decodes but carries no position |
| `cursor_stale` | — | it was issued for a different ordering or filter |
| `cursor_missing_keys` | `{missing}` | it has no value for one or more ordering keys |
| `cursor_value_mismatch` | `{name, error}` | a value in it does not fit the field's type |

```python
from fastapi_viewsets.exceptions import NotFoundError

raise NotFoundError(pk)
# unregistered: {"detail": "Item with pk 42 not found"}
# registered:   {"detail": "Item with pk 42 not found", "detail_code": "not_found", "detail_params": {"pk": 42}}
```

### Custom errors

Subclass `DfViewSetError` for an application's own errors that should carry the same shape:

```python
from fastapi_viewsets.exceptions import DfViewSetError

class InsufficientBalanceError(DfViewSetError):
    def __init__(self, required: float, available: float):
        message = f"balance {available} is short of the required {required}"
        super().__init__(402, message, "insufficient_balance", {"required": required, "available": available})
```

Registering `df_viewset_exception_handler` for `DfViewSetError` covers every subclass, this
package's own errors and an application's own alike.

## Frontend

`@dynamicforms/fastapi-viewsets/vue` converts an error body into an `ErrorDescription`: `code`,
`params`, the English `detail` and `origin: 'server'`. The interface has the shape of
`ErrorDescription` in `@dynamicforms/vue-forms`, so the application renders and translates a
server's errors with the same function as its validators' errors. The package holds no
translations. How an application converts an error into text in its own language is described in
[vue-forms: Error messages and translation](:vue-forms:/guide/getting-started.html#error-messages-and-translation);
the codes of this page are added to the application's translations next to the validators' codes.

### One error

`toErrorDescription(body)` converts the body of a failed request:

```ts
import { toErrorDescription } from '@dynamicforms/fastapi-viewsets/vue';

const error = toErrorDescription(body); // body: { detail, detail_code?, detail_params? }
// { code: 'not_found', params: { pk: 42 }, detail: 'Item with pk 42 not found', origin: 'server' }
```

| Field | Value |
|-------|-------|
| `code` | `detail_code`; an empty string where the body has none (the handler is not registered, or the error is a view's own `HTTPException`) |
| `params` | `detail_params` as the server sent them; `{}` where the body has none |
| `detail` | `detail`, unchanged |
| `origin` | `'server'` |

### Field errors

A request whose body or parameters fail validation is answered by FastAPI with status 422 and a
list of failures, one per failing value; over muxws the body is the same:

```json
{
  "detail": [
    { "type": "string_too_short", "loc": ["body", "name"], "msg": "String should have at least 3 characters", "input": "a", "ctx": { "min_length": 3 } },
    { "type": "int_parsing", "loc": ["body", "address", "zip"], "msg": "Input should be a valid integer", "input": "x" }
  ]
}
```

`toFieldErrors(body)` converts it into errors keyed by field name. The name is `loc` without its
first element (`body`, `query`, `path`, `header` or `cookie`), joined with `.`; a failure of the
body as a whole is keyed `''`. Each error has pydantic's `type` as `code`, `ctx` as `params` and
`msg` as `detail`:

```ts
import { toFieldErrors } from '@dynamicforms/fastapi-viewsets/vue';

toFieldErrors(body);
// {
//   name: [{ code: 'string_too_short', params: { min_length: 3 }, detail: 'String should have at least 3 characters', origin: 'server' }],
//   'address.zip': [{ code: 'int_parsing', params: {}, detail: 'Input should be a valid integer', origin: 'server' }],
// }
```

A field of a vue-forms `Group` bound under the same name as the model's field receives its errors:

```ts
import { toFieldErrors, type FieldErrorsBody } from '@dynamicforms/fastapi-viewsets/vue';
import { ValidationError, type Group } from '@dynamicforms/vue-forms';

function showServerErrors(form: Group, body: FieldErrorsBody) {
  for (const [name, errors] of Object.entries(toFieldErrors(body))) {
    const field = form.field(name);
    if (field) {
      field.errors = [
        ...field.errors,
        ...errors.map((e) => new ValidationError(e.code, e.params, e.detail, e.origin)),
      ];
    }
  }
}
```

The codes are pydantic's error types (`missing`, `string_too_short`, `int_parsing`, ...), and
`params` holds the values pydantic built `msg` from.

### Codes

The codes this package raises, with their English `detail`:

| `code` | `detail` | Params |
|--------|----------|--------|
| `not_found` | `Item with pk {pk} not found` | `pk` |
| `session_expired` | `Session expired or invalid` | — |
| `not_authorized` | `Not authorized to perform this action` | — |
| `rate_limited` | `Rate limit exceeded` | — |
| `unsupported_list_shape` | `unsupported list shape "{shape}"; this endpoint offers {allowed}` | `shape`, `allowed` |
| `cursor_unreadable` | `cursor is not readable: {error}` | `error` |
| `cursor_missing_position` | `cursor is not readable: no position in it` | — |
| `cursor_stale` | `this cursor was issued for a different ordering or filter - start from the first page` | — |
| `cursor_missing_keys` | `cursor has no value for ordering key(s): {missing}` | `missing` |
| `cursor_value_mismatch` | `cursor value for "{name}" does not fit the field: {error}` | `name`, `error` |

`allowed` and `missing` are arrays. An application's own `DfViewSetError` subclasses add their codes
to the same translations.
