# Migration guide

Every breaking release has its own section below, newest first. If you are crossing several
releases at once, work from the bottom of the page upwards.

<!-- New releases go directly below this comment, above the previous one, as `## Upgrading to vX.Y.Z (from vA.B.x)`. -->

## Upgrading to v0.8.0 (from v0.7.x)

The npm package no longer translates errors and no longer depends on `@dynamicforms/translatable`.
It converts an error body into an `ErrorDescription`, and the application translates it by its
`code` with the function it uses for its vue-forms errors. The Python package and the response
bodies are unchanged.

- [ ] Remove `@dynamicforms/translatable` from the application's dependencies if nothing else uses
      it.
- [ ] Remove the `translateStrings` call.
- [ ] Replace `translateApiError(body)` with the application's error text function over
      `toErrorDescription(body)`:

  ```ts
  // before
  translateStrings((key) => myTranslations[key]);
  const message = translateApiError(body);

  // after
  const message = errorText(toErrorDescription(body));
  ```

  `errorText` is the application's function described in
  [vue-forms: Error messages and translation](:vue-forms:/guide/getting-started.html#error-messages-and-translation).
  It looks the code up in the application's translations, substitutes `params` and falls back to
  `detail`. Add the codes listed in [Error codes](./error-codes#codes) to the translations; the
  placeholder names are the same.
- [ ] Remove reads of `translatableStrings`. The English text of each code is the body's `detail`.
- [ ] `params` holds the values as the server sent them. `allowed` and `missing` are arrays; format
      them in `errorText` where the translation shows them.

## Upgrading to v0.6.0 (from v0.5.7)

Nothing about a response body changes unless you opt in. There is no checklist for this release -
see [Error codes](./error-codes) for what changed and how to opt into it.

`NotFoundError` moves from `fastapi_viewsets.response_classes` to `fastapi_viewsets.exceptions`:

```python
# before
from fastapi_viewsets.response_classes import NotFoundError

# after
from fastapi_viewsets.exceptions import NotFoundError
```
