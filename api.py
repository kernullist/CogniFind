import sys
import os

# In the frozen, windowed (no-console) build, sys.stdout/stderr are None, so any
# print() or library output would crash. Redirect both to a log file before
# importing anything that might write. Done here (module top) because the frozen
# exe runs this file as its entry point.
if getattr(sys, "frozen", False):
    try:
        _log_dir = os.path.join(os.path.expanduser("~"), ".cognifind")
        os.makedirs(_log_dir, exist_ok=True)
        _log_file = open(
            os.path.join(_log_dir, "cognifind.log"),
            "a", buffering=1, encoding="utf-8", errors="replace",
        )
        sys.stdout = _log_file
        sys.stderr = _log_file
    except Exception:
        pass

import asyncio
from contextlib import asynccontextmanager
from threading import Thread, Lock
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from src.config import EMBEDDING_MODELS
from src.embedding import EmbeddingEngine
from src.database import (
    init_db,
    get_monitored_dirs,
    save_monitored_dirs,
    query_similar_documents,
    is_document_indexed,
    count_documents,
    get_active_model_key,
    set_active_model_key,
    clear_index,
)
from src.watcher import IndexingWorker, WatcherManager


qt_app = None
embedding_engine = None
worker = None
watcher = None
worker_status = "starting"
shutting_down = False

# Shared embedding-model / download state, polled by the UI via /api/status.
model_lock = Lock()
model_state = {
    "ready": False,
    "downloading": False,
    "model": None,
    "file": None,
    "percent": 0.0,
    "downloaded": 0,
    "total": 0,
    "error": None,
}


def on_status_changed(status):
    global worker_status
    worker_status = status


def on_download_progress(model_key, filename, downloaded, total):
    """Called from EmbeddingEngine during a model download."""
    with model_lock:
        model_state.update(
            downloading=True,
            ready=False,
            model=model_key,
            file=filename,
            downloaded=downloaded,
            total=total,
            percent=(downloaded / total * 100.0) if total else 0.0,
        )


def _set_model_state(**kwargs):
    with model_lock:
        model_state.update(kwargs)


def _start_indexing(engine, monitored_dirs):
    """Creates and starts the worker + watcher for the given engine/dirs."""
    global worker, watcher
    worker = IndexingWorker(engine, monitored_dirs)
    worker.status_changed.connect(on_status_changed)
    watcher = WatcherManager(monitored_dirs, worker)
    worker.start(QThread.IdlePriority)
    watcher.start()


def _init_engine_background():
    """Builds the active embedding engine (downloading if needed) off the main
    thread so the API server is responsive and can report download progress."""
    global embedding_engine, worker_status

    active_model = get_active_model_key()
    print(f"Loading embedding model '{active_model}'...")
    _set_model_state(ready=False, downloading=False, model=active_model,
                     file=None, percent=0.0, downloaded=0, total=0, error=None)
    try:
        engine = EmbeddingEngine(active_model, progress_callback=on_download_progress)
    except Exception as e:
        print(f"Failed to load model '{active_model}': {e}")
        _set_model_state(downloading=False, error=str(e))
        worker_status = "model load failed"
        return

    embedding_engine = engine
    _set_model_state(ready=True, downloading=False, percent=100.0, error=None)

    if shutting_down:
        return

    monitored_dirs = get_monitored_dirs()
    print(f"Monitored directories: {monitored_dirs}")
    # Skip if a concurrent settings/model change already started indexing, to
    # avoid leaving two workers running.
    if worker is None:
        _start_indexing(engine, monitored_dirs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global qt_app, shutting_down

    qt_app = QApplication.instance() or QApplication([])
    init_db()

    # Load the model in the background so the server starts serving immediately
    # and the UI can poll /api/status to render the download progress bar.
    Thread(target=_init_engine_background, daemon=True).start()

    print("ContextFinder API server started.")

    yield

    print("Shutting down...")
    shutting_down = True
    if watcher:
        watcher.stop()
    if worker:
        worker.stop()


app = FastAPI(title="ContextFinder API", lifespan=lifespan)

# Restrict CORS to the Tauri webview origins instead of "*":
#   - http://localhost:5173      : Vite dev server (devUrl)
#   - http://tauri.localhost     : Windows production webview
#   - tauri://localhost          : other platforms' webview
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchRequest(BaseModel):
    query: str
    date_from: str | None = None
    date_to: str | None = None
    extensions: list[str] | None = None
    # Bound the result count: limit <= 0 would make the KNN k=0 (a sqlite-vec
    # error) and an unbounded value could return a huge response.
    limit: int = Field(default=5, ge=1, le=50)


class SettingsRequest(BaseModel):
    monitored_dirs: list[str]


class ModelRequest(BaseModel):
    model_key: str


# Defined as a plain (non-async) function so FastAPI runs it in its worker
# threadpool. Embedding is CPU-bound and the DB call is blocking; running them
# directly on the event loop would stall status polling and other requests.
@app.post("/api/search")
def search(req: SearchRequest):
    if not req.query.strip():
        return []
    if embedding_engine is None:
        raise HTTPException(status_code=503, detail="Embedding model is still loading")
    try:
        query_vector = embedding_engine.get_embedding(req.query.strip(), is_query=True)
        results = query_similar_documents(
            query_vector,
            limit=req.limit,
            file_extensions=req.extensions,
            date_from=req.date_from,
            date_to=req.date_to
        )
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Non-async: it does a blocking COUNT(*) on the DB, which should run in the
# threadpool rather than on the event loop.
@app.get("/api/status")
def get_status():
    with model_lock:
        model_info = dict(model_state)
    try:
        documents = count_documents()
    except Exception:
        documents = 0
    if worker is not None:
        progress = worker.get_index_progress()
    else:
        progress = {"queued": 0, "scanning": False}
    return {
        "status": worker_status,
        "model": model_info,
        "index": {
            "documents": documents,
            "queued": progress["queued"],
            "scanning": progress["scanning"],
        },
    }


@app.get("/api/settings")
async def get_settings():
    dirs = get_monitored_dirs()
    return {"monitored_dirs": dirs}


# Non-async: stopping the worker blocks on QThread.wait(), which must not run
# on the event loop. FastAPI dispatches sync handlers to the threadpool.
@app.put("/api/settings")
def update_settings(req: SettingsRequest):
    global worker, watcher

    if embedding_engine is None:
        raise HTTPException(status_code=503, detail="Embedding model is still loading")

    save_monitored_dirs(req.monitored_dirs)

    if watcher:
        watcher.stop()
    if worker:
        worker.stop()
        worker.wait()
        # Disconnect the old worker's signal before dropping it so the slot is
        # not left bound to a discarded object.
        try:
            worker.status_changed.disconnect(on_status_changed)
        except (RuntimeError, TypeError):
            pass

    monitored_dirs = req.monitored_dirs
    _start_indexing(embedding_engine, monitored_dirs)

    return {"monitored_dirs": monitored_dirs}


@app.get("/api/model")
async def get_model():
    return {
        "active": get_active_model_key(),
        "available": [
            {"key": k, "label": v["label"], "dim": v["dim"]}
            for k, v in EMBEDDING_MODELS.items()
        ],
    }


# Non-async: rebuilding the engine may download a model and stopping the worker
# blocks on QThread.wait(); both must run off the event loop.
@app.put("/api/model")
def set_model(req: ModelRequest):
    global embedding_engine, worker, watcher

    # Refuse until the initial engine load has finished, so this cannot race the
    # background init and leave two workers / a wrong engine running.
    if embedding_engine is None:
        raise HTTPException(status_code=503, detail="Embedding model is still loading")

    if req.model_key not in EMBEDDING_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model key: {req.model_key}")

    if req.model_key == get_active_model_key():
        return {"active": req.model_key, "changed": False}

    # Build the new engine first (may download the model; progress is reported
    # via /api/status). If this fails we have not yet touched the existing index,
    # and the old engine keeps serving search.
    _set_model_state(ready=False, downloading=False, model=req.model_key,
                     file=None, percent=0.0, downloaded=0, total=0, error=None)
    try:
        new_engine = EmbeddingEngine(req.model_key, progress_callback=on_download_progress)
    except Exception as e:
        _set_model_state(downloading=False, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to load model: {e}")
    _set_model_state(ready=True, downloading=False, percent=100.0, error=None)

    # Stop indexing before swapping the engine and wiping the index.
    if watcher:
        watcher.stop()
    if worker:
        worker.stop()
        worker.wait()
        try:
            worker.status_changed.disconnect(on_status_changed)
        except (RuntimeError, TypeError):
            pass

    # Persist the choice, wipe the now-incompatible index, recreate the vec table
    # sized for the new model, and re-index from scratch.
    set_active_model_key(req.model_key)
    clear_index(new_engine.dim)
    embedding_engine = new_engine

    _start_indexing(embedding_engine, get_monitored_dirs())

    return {"active": req.model_key, "changed": True}


@app.post("/api/index/scan")
async def trigger_rescan():
    if worker:
        worker.scan_and_sync()
    return {"ok": True}


@app.post("/api/open-file")
def open_file(path: str):
    # Only launch files we actually indexed. CORS is open, so without this any
    # local origin could trigger os.startfile on an arbitrary path.
    if not is_document_indexed(path):
        raise HTTPException(status_code=403, detail="Path is not an indexed document")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        os.startfile(path)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
