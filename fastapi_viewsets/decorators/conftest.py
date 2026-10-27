def iter_routes(routes):
    """
    Flatten a FastAPI route list, recursing into included routers.

    fastapi>=0.137 wraps each include_router() in an object exposing `.original_router` (duck-typed here).
    """
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from iter_routes(original_router.routes)
        else:
            yield route
