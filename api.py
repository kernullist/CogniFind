import sys
import os
import asyncio
from contextlib import asynccontextmanager
from threading import Thread
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
    get_active_model_key,
    set_active_model_key,
    clear_index,
)
from src.watcher import IndexingWorker, WatcherManager


qt_app = None
embedding_engine = None
worker = None
watcher = None
worker_status = "idle"


def on_status_changed(status):
    global worker_status
    worker_status = status


@asynccontextmanager
async def lifespan(app: FastAPI):
    global qt_app, embedding_engine, worker, watcher, worker_status

    qt_app = QApplication.instance() or QApplication([])

    init_db()

    active_model = get_active_model_key()
    print(f"Pre-heating Embedding Engine (model: {active_model})...")
    embedding_engine = EmbeddingEngine(active_model)

    monitored_dirs = get_monitored_dirs()
    print(f"Monitored directories: {monitored_dirs}")

    worker = IndexingWorker(embedding_engine, monitored_dirs)
    worker.status_changed.connect(on_status_changed)

    watcher = WatcherManager(monitored_dirs, worker)

    worker.start(QThread.IdlePriority)
    watcher.start()

    print("ContextFinder API server started.")

    yield

    print("Shutting down...")
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


@app.get("/api/status")
async def get_status():
    return {"status": worker_status}


@app.get("/api/settings")
async def get_settings():
    dirs = get_monitored_dirs()
    return {"monitored_dirs": dirs}


# Non-async: stopping the worker blocks on QThread.wait(), which must not run
# on the event loop. FastAPI dispatches sync handlers to the threadpool.
@app.put("/api/settings")
def update_settings(req: SettingsRequest):
    global worker, watcher

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
    worker = IndexingWorker(embedding_engine, monitored_dirs)
    worker.status_changed.connect(on_status_changed)
    watcher = WatcherManager(monitored_dirs, worker)
    worker.start(QThread.IdlePriority)
    watcher.start()

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

    if req.model_key not in EMBEDDING_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model key: {req.model_key}")

    if req.model_key == get_active_model_key():
        return {"active": req.model_key, "changed": False}

    # Build the new engine first (may download the model). If this fails we have
    # not yet touched the existing index.
    try:
        new_engine = EmbeddingEngine(req.model_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load model: {e}")

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

    monitored_dirs = get_monitored_dirs()
    worker = IndexingWorker(embedding_engine, monitored_dirs)
    worker.status_changed.connect(on_status_changed)
    watcher = WatcherManager(monitored_dirs, worker)
    worker.start(QThread.IdlePriority)
    watcher.start()

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
