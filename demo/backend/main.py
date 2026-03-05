from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from demo.backend.celery_app import redis_async
from demo.backend.viewsets import MusicTrackViewSet
from fastapi_viewsets.decorators.celery_viewset import start_result_reader, stop_result_reader
from fastapi_viewsets.decorators.route_viewset import route_viewset


@asynccontextmanager
async def lifespan(app: FastAPI):
    from fastapi_viewsets.decorators.celery_viewset import get_result_queue_key
    queue_key = get_result_queue_key("music")
    await start_result_reader(redis_async, queue_key)
    yield
    await stop_result_reader()


app = FastAPI(title="Demo ViewSet App", lifespan=lifespan)
router = APIRouter()

route_viewset(router, base_path="/music", pk_field_name="id")(MusicTrackViewSet)

app.include_router(router)

def main():
    import uvicorn
    print("Starting demo application at http://127.0.0.1:8000")
    print("Documentation is available at http://127.0.0.1:8000/docs")
    uvicorn.run(app, host="127.0.0.1", port=8000)
