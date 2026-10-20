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
nested inside `detail` itself: `detail` is a plain string, and `detail_code` and `detail_params`
appear only once the application registers the handler:

```python
from fastapi_viewsets.exceptions import DfViewSetError, df_viewset_exception_handler

app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)
```

An application that never registers this receives `{"detail": "..."}`, from FastAPI's own default
`HTTPException` handling. A view's own `raise HTTPException(status_code,
detail="...")` is unaffected either way - only exceptions descending from `DfViewSetError` carry a
`code`.

The server never picks a locale for an error: `detail` is always English, and the client shows the
error in its own language by its `code`.

The handler is registered on the application, so it shapes a `DfViewSetError` over REST and over
[muxws](./muxws) alike.

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
| `ERROR_NAMESPACE_PYDANTIC` | `pydantic` | pydantic's error types, which [`toFieldErrors()`](#field-errors) forwards, e.g. `pydantic.string_too_short` |

```python
from fastapi_viewsets import ERROR_NAMESPACE, ERROR_NAMESPACE_PYDANTIC
```

```ts
import { ERROR_NAMESPACE, ERROR_NAMESPACE_PYDANTIC } from '@dynamicforms/fastapi-viewsets';
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
# unregistered: {"detail": "Item with pk 42 not found"}
# registered:   {"detail": "Item with pk 42 not found", "detail_code": "fastapi-viewsets.not_found", "detail_params": {"pk": 42}}
```

### Executor outcomes

The errors that end a call to a `celery_viewset` action without its result, by `reason` (see
[Outcomes of a call](./routers#outcomes-of-a-call)):

| `code` | `reason` | The action |
|--------|----------|------------|
| `fastapi-viewsets.action_dispatch_failed` | `send` | was not sent: the broker is unreachable, or the message cannot be encoded |
| | `serialization` | was not sent: its arguments, context or request cannot be serialized |
| | `unknown_task` | did not run: the worker has no task of that name |
| | `rejected` | did not run: the worker rejected the message |
| | `revoked`, `expired` | did not run: it was revoked, or expired, before it started |
| `fastapi-viewsets.action_interrupted` | `time_limit` | exceeded its soft or hard time limit (504) |
| | `terminated` | was terminated (503) |
| | `lost` | lost the worker process executing it (503) |
| `fastapi-viewsets.action_failed` | `error` | raised an error that is not an `HTTPException`; `detail` names the task only, the error is logged in the worker |
| | `result` | returned a result that cannot be serialized, or that the caller cannot deserialize |
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

Registering `df_viewset_exception_handler` for `DfViewSetError` covers every subclass, this
package's own errors and an application's own alike.

An application's own subclass raised by a `celery_viewset` action reaches the FastAPI process as a
`DfViewSetError` with the same status, `detail`, `code`, `params` and headers; only the classes
`fastapi_viewsets.exceptions` defines are rebuilt as their own class.

The error travels from the worker as JSON fields, in the result payload of the call, and an
[idempotency](./idempotency) record stores it the same way. In those fields the `code` of an error of a class
`fastapi_viewsets.exceptions` defines (other than `DfViewSetError` itself) is written without the namespace, as
`not_found`, and the reading process adds `ERROR_NAMESPACE` back when it rebuilds the error. The code of any other
error is written as given. A worker and a FastAPI process of different versions of this package read each other's
payloads, and a stored record keeps its meaning across an upgrade.

## Frontend

`@dynamicforms/fastapi-viewsets` converts an error body into an `ErrorDescription`: `code`,
`params`, the English `detail` and `origin: 'server'`. The interface has the shape of
`ErrorDescription` in `@dynamicforms/vue-forms`, so the application renders and translates a
server's errors with the same function as its validators' errors. The package holds no
translations. How an application converts an error into text in its own language is described in
[vue-forms: Error messages and translation](:vue-forms:/api/validators.html#error-messages-and-translation);
the codes of this page are added to the application's translations under the prefixes of their namespaces (see
[Translating the codes](#translating-the-codes)).

### One error

`toErrorDescription(body)` converts the body of a failed request:

```ts
import { toErrorDescription } from '@dynamicforms/fastapi-viewsets';

const error = toErrorDescription(body); // body: { detail, detail_code?, detail_params? }
// { code: 'fastapi-viewsets.not_found', params: { pk: 42 }, detail: 'Item with pk 42 not found', origin: 'server' }
```

| Field | Value |
|-------|-------|
| `code` | `detail_code`, unchanged; an empty string where the body has none (the handler is not registered, or the error is a view's own `HTTPException`) |
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
body as a whole is keyed `''`. Each error has pydantic's `type` in the namespace `ERROR_NAMESPACE_PYDANTIC` as
`code` (`pydantic.string_too_short`), `ctx` as `params` and `msg` as `detail`:

```ts
import { toFieldErrors } from '@dynamicforms/fastapi-viewsets';

toFieldErrors(body);
// {
//   name: [{ code: 'pydantic.string_too_short', params: { min_length: 3 }, detail: 'String should have at least 3 characters', origin: 'server' }],
//   'address.zip': [{ code: 'pydantic.int_parsing', params: {}, detail: 'Input should be a valid integer', origin: 'server' }],
// }
```

A name is a path through the members of a vue-forms form: a `Group` member by its name, a `List`
row by its index. The errors keyed `''` belong to the form itself, and so do the errors of a name
the form does not hold. A `SubmitAction` sends the form through a ViewSet and, where the server
refuses it, writes the server's errors to the form before it throws:

```ts
import {
  type ApiErrorBody,
  type ErrorDescription,
  type FieldErrorsBody,
  toErrorDescription,
  toFieldErrors,
} from '@dynamicforms/fastapi-viewsets';
import { Action, type FieldBase, Group, List, SubmitAction, ValidationError } from '@dynamicforms/vue-forms';

function elementAt(root: FieldBase, name: string): FieldBase | undefined {
  let element: FieldBase | null | undefined = root;
  for (const key of name ? name.split('.') : []) {
    if (element instanceof Group) element = element.field(key);
    else if (element instanceof List) element = element.get(Number(key));
    else return undefined;
  }
  return element ?? undefined;
}

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
        } catch (error: any) {
          const status = error.response?.status;
          const body = error.response?.data;
          if (status === 422) {
            for (const [name, errors] of Object.entries(toFieldErrors(body as FieldErrorsBody))) {
              addErrors(elementAt(target, name) ?? target, errors);
            }
          } else if (typeof body?.detail === 'string') {
            addErrors(target, [toErrorDescription(body as ApiErrorBody)]);
          }
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

The codes are pydantic's error types in the namespace `pydantic` (`pydantic.missing`,
`pydantic.string_too_short`, `pydantic.int_parsing`, ...), and `params` holds the values pydantic built `msg` from.
`toFieldErrors()` prefixes `type` as it arrives. The type of a `PydanticCustomError` is the application's own string;
one that contains a `.` or is not snake_case gives a code outside the grammar above.

### Codes

The codes this package raises, with their English `detail`:

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

`allowed` and `missing` are arrays. An application's own `DfViewSetError` subclasses add their codes
to the translations under the prefix of their namespace.

### Translating the codes

The application maps each namespace to a translation key prefix of its own and the code after the namespace to a key
under that prefix, taking a library's namespace from its constant. Here `fastapi-viewsets.not_found` renders through
the key `libs.fastapi-viewsets.not_found`, `pydantic.string_too_short` through `libs.pydantic.string_too_short` and
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
    "fastapi-viewsets": { "not_found": "Element {pk} ne obstaja" },
    "pydantic": { "string_too_short": "Vnesite vsaj {min_length} znake" }
  },
  "errors": { "insufficient_balance": "Stanje {available} ne zadošča za {required}" }
}
```

An error without a code (`code` is `''`) has no key and renders as its `detail`. vue-forms' codes are mapped the same
way through vue-forms' own `ERROR_NAMESPACE`.
