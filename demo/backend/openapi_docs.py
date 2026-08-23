"""
A "Try it in Swagger UI" link on every operation, for the ReDoc page.

ReDoc renders the API beautifully and cannot execute a single request; Swagger UI executes requests
and reads less well. Rather than pick one, the schema carries a deep link from each operation to
its Swagger UI entry, and each page hides what belongs to the other.

Both pages render the *same* schema, so the link cannot be added for one and omitted for the
other - it is emitted once and hidden with CSS in Swagger UI, where it would only point at itself.

Adapted from the same mechanism in a production application; the fiddly parts are load-bearing:

* The class goes on a wrapping `<div>`, never on the `<a>`. Swagger UI's markdown sanitiser strips
  `class` from anchors specifically, so a `<a class="...">` arrives unstyled and unhideable.
* The hiding rule also matches by `href` prefix, because that is the one attribute that has to
  survive - a link without it would not be a link.
* FastAPI's built-in `docs_url`/`redoc_url` have no hook for extra `<head>` content, so both pages
  are served by hand.
"""

import re

from typing import Any

TRY_BUTTON_CLASS = "api-try-button"

REDOC_HEAD = """
<style>
  .api-try-button { margin-top: .4em; }
  .api-try-button a, a[href^="/docs#/"] {
    display: inline-block;
    padding: .3em .9em;
    background: linear-gradient(45deg, #1f6feb, #7bb0f0);
    color: #fff !important;
    border-radius: .5em;
    font-size: .85em;
    font-weight: 600;
    text-decoration: none !important;
  }
  .api-try-button a:hover, a[href^="/docs#/"]:hover {
    background: linear-gradient(45deg, #4a90e2, #a8d0f8);
  }
</style>
"""

SWAGGER_HEAD = """
<style>
  /* Matched by href as well as by class: per-operation descriptions go through a stricter
     sanitiser than the top-level one, and the wrapping div's class does not always survive it. */
  .api-try-button, a[href^="/docs#/"] { display: none !important; }
</style>
"""


def _anchor(tag: str, operation_id: str) -> str:
    """
    Swagger UI's own deep-link slug rule, as far as it can be mirrored from outside: anything that
    is not a word character or a hyphen becomes an underscore. If it ever stops matching, the link
    still opens Swagger UI - just not scrolled to the operation.
    """
    slug = re.sub(r"[^\w-]", "_", tag)
    return f"/docs#/{slug}/{operation_id}"


def add_try_links(schema: dict[str, Any]) -> dict[str, Any]:
    for methods in schema.get("paths", {}).values():
        for method, operation in methods.items():
            if method == "parameters":
                continue
            tags = operation.get("tags")
            operation_id = operation.get("operationId")
            if not tags or not operation_id:
                continue
            href = _anchor(tags[0], operation_id)
            operation["description"] = (
                f"{operation.get('description') or ''}\n\n"
                f'<div class="{TRY_BUTTON_CLASS}">'
                f'<a href="{href}" target="_blank" rel="noopener">Try it in Swagger UI ↗</a></div>'
            ).strip()
    return schema
