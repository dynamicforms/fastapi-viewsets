# Migration guide

Every breaking release has its own section below, newest first. If you are crossing several
releases at once, work from the bottom of the page upwards.

<!-- New releases go directly below this comment, above the previous one, as `## Upgrading to vX.Y.Z (from vA.B.x)`. -->

## Upgrading to v0.8.0 (from v0.7.x)

The npm package moves to `@dynamicforms/translatable` `^0.3.0`. The Python package and the response
bodies are unchanged.

- [ ] Install `@dynamicforms/translatable@^0.3.0`.
- [ ] Pass the application's translation function to `translateStrings`, with the namespace that
      holds the error codes in the application's translations:

  ```ts
  // before
  translateStrings((key, defaultValue) => myTranslations[key]);

  // after (vue-i18n)
  translateStrings(i18n.global.t, 'errors');
  ```

  ```json
  { "errors": { "not_found": "Element s ključem {pk} ne obstaja" } }
  ```

  The translation keeps the `{name}` placeholders; the translation function substitutes
  `detail_params` into them.
- [ ] Remove reads of `translatableStrings`. The English defaults are listed in
      [Error codes](./error-codes#frontend).
- [ ] Call `translateApiError` where the message is rendered (a template or a computed) if the
      message must follow a locale switch; the returned string is in the locale current at the
      call.

Codes an application raises through its own `DfViewSetError` subclasses are translated through the
same function. Without a translation, their `detail` is shown, as before.

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
