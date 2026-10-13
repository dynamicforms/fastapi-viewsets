# Migration guide

Every breaking release has its own section below, newest first. If you are crossing several
releases at once, work from the bottom of the page upwards.

<!-- New releases go directly below this comment, above the previous one, as `## Upgrading to vX.Y.Z (from vA.B.x)`. -->

## Upgrading to v0.8.0 (from v0.7.x)

The npm package no longer translates errors and no longer depends on `@dynamicforms/translatable`.
It converts an error body into an `ErrorDescription`, and the application translates it by its
`code` with the function it uses for its vue-forms errors. The response bodies are unchanged.

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
  [vue-forms: Error messages and translation](:vue-forms:/api/validators.html#error-messages-and-translation).
  It looks the code up in the application's translations, substitutes `params` and falls back to
  `detail`. Add the codes listed in [Error codes](./error-codes#codes) to the translations; the
  placeholder names are the same.
- [ ] Remove reads of `translatableStrings`. The English text of each code is the body's `detail`.
- [ ] `params` holds the values as the server sent them. `allowed` and `missing` are arrays; format
      them in `errorText` where the translation shows them.
- [ ] Deploy the FastAPI process and the Celery workers together when the application uses
      `JWTAuthBackend` with `celery_viewset`: the user crosses to the worker as the token and the
      position of its backend in `settings.viewsets_auth_processors`, which the worker reads from
      its own settings. A worker of 0.7.x cannot read the new recipe, and a 0.8.0 worker cannot read
      the old one.
- [ ] A `perform_update` that applies a `PATCH` reads the fields the request stated with
      `data.model_dump(exclude_unset=True)`; the body no longer carries the primary key or the
      fields it left out.
- [ ] A ViewSet with custom endpoints only can be built with `restViewSet<T>()(pk, [])`; the schema
      check matches custom endpoints by the BE method's name (`top_rated` → `topRated()` or
      `top_rated()`), so a FE method named after a hyphenated path segment is reported until it is
      renamed.

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
