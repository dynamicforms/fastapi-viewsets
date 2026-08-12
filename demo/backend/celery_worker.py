import os

# Running the worker at all means the Celery path is wanted, so it is switched on here rather than
# relying on the environment - see DEMO_CELERY in demo/backend/viewsets.py. Must be set before
# viewsets is imported, since the decorator is applied at import time.
os.environ.setdefault("DEMO_CELERY", "1")

import demo.backend.viewsets  # noqa: E402, F401 — registers MusicTrackViewSet tasks on celery_app

from demo.backend.celery_app import celery_app  # noqa: E402

app = celery_app
