"""
A "Try it in Swagger UI" link on every operation, for the ReDoc page. Both pages render the same
schema, so the link is hidden with CSS in Swagger UI. The class goes on a wrapping `<div>`: Swagger
UI's sanitiser strips `class` from anchors. Both pages are served by hand, as FastAPI's `docs_url`
and `redoc_url` take no extra `<head>` content.
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
  /* Matched by href too: the stricter per-operation sanitiser may strip the div's class. */
  .api-try-button, a[href^="/docs#/"] { display: none !important; }
</style>
"""


def _anchor(tag: str, operation_id: str) -> str:
    """Swagger UI's deep link: a tag character other than a word character or hyphen becomes `_`."""
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
