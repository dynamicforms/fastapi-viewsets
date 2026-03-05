import multiprocessing
import subprocess


def run_celery():
    subprocess.run(
        ["celery", "-A", "demo.backend.celery_worker", "worker", "--loglevel=info", "--concurrency=4"],  # noqa: S607
        check=False,
    )

def run_fe():
    subprocess.run(["npm", "run", "demo:dev"], check=False)  # noqa: S607

def run_fastapi():
    import uvicorn
    uvicorn.run("demo.backend.main:app", host="127.0.0.1", port=8000, reload=False)

if __name__ == "__main__":
    print("Starting Celery worker and FastAPI server...")
    print("FastAPI docs: http://127.0.0.1:8000/docs")
    celery_proc = multiprocessing.Process(target=run_celery, daemon=True)
    celery_proc.start()
    fe_proc = multiprocessing.Process(target=run_fe, daemon=True)
    fe_proc.start()
    try:
        run_fastapi()
    except KeyboardInterrupt:
        pass
    finally:
        celery_proc.terminate()
        celery_proc.join()
        fe_proc.terminate()
        fe_proc.join()
        print("Stopped.")
