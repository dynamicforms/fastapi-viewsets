import demo.backend.viewsets  # noqa: F401 — registers ItemViewSet tasks on celery_app

from demo.backend.celery_app import celery_app

app = celery_app
