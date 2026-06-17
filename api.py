import sys
import os
import asyncio
from contextlib import asynccontextmanager
from threading import Thread
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from src.embedding import EmbeddingEngine
from src.database import (
    init_db,
    get_monitored_dirs,
    save_monitored_dirs,
    query_similar_documents,
    is_document_indexed,
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

    print("Pre-heating Embedding Engine...")
    embedding_engine = EmbeddingEngine()

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchRequest(BaseModel):
    query: str
    date_from: str | None = None
    date_to: str | None = None
    extensions: list[str] | None = None
    limit: int = 5


class SettingsRequest(BaseModel):
    monitored_dirs: list[str]


# Defined as a plain (non-async) function so FastAPI runs it in its worker
# threadpool. Embedding is CPU-bound and the DB call is blocking; running them
# directly on the event loop would stall status polling and other requests.
@app.post("/api/search")
def search(req: SearchRequest):
    if not req.query.strip():
        return []
    try:
        query_vector = embedding_engine.get_embedding(req.query.strip())
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

    monitored_dirs = req.monitored_dirs
    worker = IndexingWorker(embedding_engine, monitored_dirs)
    worker.status_changed.connect(on_status_changed)
    watcher = WatcherManager(monitored_dirs, worker)
    worker.start(QThread.IdlePriority)
    watcher.start()

    return {"monitored_dirs": monitored_dirs}


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
