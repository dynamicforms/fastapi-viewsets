# Migration guide

Every breaking release has its own section below, newest first. If you are crossing several
releases at once, work from the bottom of the page upwards.

<!-- New releases go directly below this comment, above the previous one, as `## Upgrading to vX.Y.Z (from vA.B.x)`. -->

## Upgrading to v0.6.0 (from v0.5.7)

Every `HTTPException` this package raises on its own now puts a structured object under `detail`
instead of a plain string. There is a [checklist](#checklist-for-0-6-0) at the end of this section.

### `detail` is an object, not a string

```python
# before
raise HTTPException(status_code=404, detail="Item with pk 5 not found")

# after
raise HTTPException(status_code=404, detail={
    "message": "Item with pk 5 not found",
    "code": "not_found",
    "params": {"pk": 5},
})
```

This applies to every error the package raises on its own behalf: not-found (404),
session-expired (401), not-authorized (403), rate-limited (429), an unsupported list shape (422),
and every cursor-pagination error (400). A view you write yourself that raises its own
`HTTPException(status_code, detail="...")` is entirely unaffected - `detail` stays whatever you
put there.

`message` is the English default, fully interpolated - a plain HTTP client with no translation
layer of its own can still show it exactly as before. `code` is new: a stable identifier,
independent of `message`'s wording, for a frontend to switch on or look up its own translation
for. `params` are the raw values `message` was interpolated with, so a translation-aware frontend
can re-interpolate its own translated template with them instead of parsing them back out of
English prose.

### The Vue client

```typescript
// before
catch (error) {
  if (error instanceof ViewSetRequestError) {
    showToast(error.response.data.detail); // a string
  }
}

// after
import { isApiErrorDetail, translateApiError } from '@dynamicforms/fastapi-viewsets';

catch (error) {
  if (error instanceof ViewSetRequestError) {
    const detail = error.response.data.detail;
    showToast(isApiErrorDetail(detail) ? translateApiError(detail) : String(detail));
  }
}
```

`translateApiError()` returns the English default unchanged unless the application has called
`translateStrings()` (also exported) for the code in question - untranslated, existing behaviour
is unchanged from a user's point of view; the only real change is the shape of the value your own
code reads `detail` as.

### Checklist for 0.6.0

1. Search your frontend for `.data.detail` (or wherever you read the body of a failed request) and
   check whether it assumes a plain string. Where it does, either switch to `isApiErrorDetail()` /
   `translateApiError()`, or read `.message` off the object for the same text the string used to
   carry.
2. If you serialize/log `error.response.data.detail` anywhere expecting a string (a Sentry breadcrumb,
   a log line), confirm it still reads sensibly now that it may be an object - `JSON.stringify` it
   if a plain-text sink needs one.
3. Nothing changes for a view that raises its own `HTTPException` with a plain string `detail` -
   no action needed there.
