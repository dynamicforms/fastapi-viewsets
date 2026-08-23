def iter_routes(routes):
    """Flatten a FastAPI route list, recursing into an included router's own .routes.

    fastapi>=0.137 wraps each include_router() call in an internal object exposing
    .original_router instead of the route directly; older fastapi puts the real route in the
    list itself. Duck-typed on the .original_router attribute rather than importing fastapi's
    internal class, so this keeps working if that class is renamed or moved again.
    """
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from iter_routes(original_router.routes)
        else:
            yield route
