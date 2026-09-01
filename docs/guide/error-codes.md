# Error Codes

Every `HTTPException` this package raises on its own behalf carries a stable `code` and the
`params` its English `detail` was built from, alongside `detail` itself. This page covers why that
exists, how to opt into it on the backend, and how to translate it on the frontend.

## Design

`detail` is plain English text meant for a developer reading a response in a browser or a log, not
for showing to an end user in their own language. Translating it would mean either the server
picking a locale for a client it does not know, or an application parsing English sentences to
recover what went wrong - both wrong. A `code` names the failure independently of the wording, and
`params` are the values that wording was interpolated with, so a client can rebuild the message in
any language without ever reading `detail`.

Nesting this under `detail` (`{"detail": {"code": ..., "params": ...}}`) would have replaced a
plain string with an object for every existing consumer, breaking every integration that reads
`detail` as text. Instead `code` and `params` are additive, top-level siblings - `detail_code` and
`detail_params` - and appear only once the application opts in:

```python
from fastapi_viewsets.exceptions import DfViewSetError, df_viewset_exception_handler

app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)
```

An application that never registers this sees exactly what it always has, `{"detail": "..."}`,
from FastAPI's own default `HTTPException` handling. A view's own `raise HTTPException(status_code,
detail="...")` is unaffected either way - only exceptions descending from `DfViewSetError` carry a
`code`.

Translation itself stays out of the server for the same reason a request is stateless: there is no
per-request locale to hold, and a mutable global translation table would make one request's
language bleed into another's. The server always answers in English; only the client, which knows
which user it is answering, decides what language to show.

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

`@dynamicforms/fastapi-viewsets/vue` exports a matching table of English defaults, keyed by
`code`, and a function that rebuilds the message from `detail_code`/`detail_params`:

```ts
import { translateApiError } from '@dynamicforms/fastapi-viewsets/vue';

const body = await response.json(); // { detail, detail_code?, detail_params? }
const message = translateApiError(body);
```

`translateApiError` returns `body.detail` unchanged whenever `detail_code` is absent - the handler
was never registered, or this is a view's own plain-string error - or names a code the table below
does not cover.

To translate into another language, supply the application's own strings the same way every other
`@dynamicforms` package does, through `translateStrings`:

```ts
import { translateStrings } from '@dynamicforms/fastapi-viewsets/vue';

translateStrings({
  not_found: 'Element s ključem {pk} ne obstaja',
  session_expired: 'Seja je potekla ali ni veljavna',
});
```

The full table of codes and their English defaults:

| `code` | Default text | Params |
|--------|--------------|--------|
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

A custom error's own `code` reaches the same table - pass it to `translateStrings` alongside the
built-in ones, keyed the same way.
