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


def _start_parent_watchdog():
    # If CogniFind (the parent) dies for any reason -- including a crash or a
    # Task Manager kill that bypasses the normal shutdown path -- terminate this
    # backend so no orphaned process keeps holding port 8765. The parent PID is
    # passed via COGNIFIND_PARENT_PID. Windows-only; a no-op elsewhere.
    pid_str = os.environ.get("COGNIFIND_PARENT_PID")
    if not pid_str or sys.platform != "win32":
        return
    try:
        ppid = int(pid_str)
    except ValueError:
        return

    import ctypes
    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, ppid)
    if not handle:
        # Could not open the parent (already gone or access denied). Do not
        # self-kill on a transient open failure right at startup.
        return

    def _wait():
        # Blocks until the parent process exits, then exits immediately.
        kernel32.WaitForSingleObject(handle, INFINITE)
        os._exit(0)

    Thread(target=_wait, daemon=True).start()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from src.config import EMBEDDING_MODELS, DEFAULT_MODEL_KEY
from src.embedding import EmbeddingEngine
from src.database import (
    init_db,
    get_monitored_dirs,
    save_monitored_dirs,
    query_similar_documents,
    is_document_indexed,
    count_documents,
    purge_documents_outside,
    get_active_model_key,
    set_active_model_key,
    get_index_model,
    set_index_model,
    get_setting,
    clear_index,
)
from src.watcher import IndexingWorker, WatcherManager


qt_app = None
embedding_engine = None
worker = None
watcher = None
worker_status = "starting"
shutting_down = False

# Serializes all worker/watcher lifecycle changes (startup init, settings change,
# model switch) so concurrent requests cannot leave two workers running.
worker_lock = Lock()

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


def _stop_indexing():
    """Stops the running worker + watcher. Caller must hold worker_lock."""
    global worker, watcher
    if watcher is not None:
        watcher.stop()
        watcher = None
    if worker is not None:
        worker.stop()
        worker.wait()
        worker = None


def _start_indexing(engine, monitored_dirs):
    """Creates and starts the worker + watcher. Caller must hold worker_lock."""
    global worker, watcher
    worker = IndexingWorker(engine, monitored_dirs)
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

    with worker_lock:
        # A concurrent settings/model change may have already started indexing.
        if worker is not None or shutting_down:
            return

        # If the existing index was built with a different model, its vectors are
        # not comparable with the active model's -- re-index. The model the index
        # was built with comes from the recorded index_model, falling back to the
        # persisted embedding_model (which tracks it), then the historical
        # default. This makes a Korean system switch an old (minilm) index over to
        # the Korean model on first run, without re-indexing a matching one.
        index_model = get_index_model() or get_setting("embedding_model") or DEFAULT_MODEL_KEY
        if count_documents() > 0 and index_model != active_model:
            print(f"Index was built with '{index_model}', active model is '{active_model}'; clearing for re-index.")
            clear_index(engine.dim)
        set_index_model(active_model)

        monitored_dirs = get_monitored_dirs()
        print(f"Monitored directories: {monitored_dirs}")
        _start_indexing(engine, monitored_dirs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global qt_app, shutting_down

    qt_app = QApplication.instance() or QApplication([])
    init_db()

    # Load the model in the background so the server starts serving immediately
    # and the UI can poll /api/status to render the download progress bar.
    Thread(target=_init_engine_background, daemon=True).start()

    print("CogniFind API server started.")

    yield

    print("Shutting down...")
    shutting_down = True
    with worker_lock:
        _stop_indexing()


API_HOST = "127.0.0.1"
API_PORT = 8765

app = FastAPI(title="CogniFind API", lifespan=lifespan)

# Restrict CORS to the Tauri webview origins instead of "*":
#   - http://tauri.localhost     : Windows production webview
#   - tauri://localhost          : other platforms' webview
#   - http://localhost:5173      : Vite dev server (devUrl), dev only
# The Vite origin is NOT trusted in the shipped app: 5173 is Vite's default
# port, so any unrelated dev server the user runs would otherwise get full
# read access to search results. It is allowed when running from source, or
# when a debug Tauri build launches the frozen sidecar (COGNIFIND_DEV_ORIGIN=1).
_PROD_ORIGINS = {
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
}
_DEV_ORIGIN = "http://localhost:5173"


def compute_allowed_origins(frozen: bool, env) -> frozenset:
    allow_dev = (not frozen) or env.get("COGNIFIND_DEV_ORIGIN") == "1"
    return frozenset(_PROD_ORIGINS | ({_DEV_ORIGIN} if allow_dev else set()))


ALLOWED_ORIGINS = compute_allowed_origins(getattr(sys, "frozen", False), os.environ)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Host header allowlist. Binding to loopback is not enough: a web page can
# DNS-rebind its own hostname to 127.0.0.1 and then talk to this server as a
# same-origin peer, which bypasses CORS entirely and would let it read search
# results (document text). Browsers always send the attacker's hostname in
# Host, so rejecting unknown hosts closes that hole.
ALLOWED_HOSTS = frozenset({
    f"{API_HOST}:{API_PORT}", f"localhost:{API_PORT}", API_HOST, "localhost",
})


# Registered after CORSMiddleware, so it wraps it and runs first.
@app.middleware("http")
async def local_only_guard(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    if host not in ALLOWED_HOSTS:
        print(f"Rejected request with Host '{host}'")
        return JSONResponse(status_code=421, content={"detail": "Invalid Host header"})
    # CORS only hides responses; "simple" cross-site requests (e.g. a form POST
    # to /api/open-file) are still delivered and executed. Browsers attach
    # Origin to every cross-origin POST/PUT, so refuse foreign origins outright.
    # Requests without Origin (tray reqwest, curl, same-origin GET) pass.
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        print(f"Rejected request from Origin '{origin}'")
        return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
    return await call_next(request)


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
        query = req.query.strip()
        query_vector = embedding_engine.get_embedding(query, is_query=True)
        results = query_similar_documents(
            query,
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
    # Snapshot the global: a settings/model change on another thread can set
    # worker to None between the check and the method calls, which would raise
    # and surface as an intermittent 500 on this poll endpoint.
    w = worker
    if w is not None:
        progress = w.get_index_progress()
        status = w.current_status
    else:
        progress = {"queued": 0, "scanning": False}
        status = worker_status
    return {
        "status": status,
        "model": model_info,
        "index": {
            "documents": documents,
            "queued": progress["queued"],
            "scanning": progress["scanning"],
        },
    }


@app.get("/api/settings")
def get_settings():
    dirs = get_monitored_dirs()
    return {"monitored_dirs": dirs}


# Non-async: stopping the worker blocks on QThread.wait(), which must not run
# on the event loop. FastAPI dispatches sync handlers to the threadpool.
@app.put("/api/settings")
def update_settings(req: SettingsRequest):
    if embedding_engine is None:
        raise HTTPException(status_code=503, detail="Embedding model is still loading")

    with worker_lock:
        # Stored form: normalized, duplicates and nested folders removed.
        dirs = save_monitored_dirs(req.monitored_dirs)
        _stop_indexing()

        # Drop documents that are no longer under any monitored folder, so
        # removing a folder also removes its documents from search.
        purged = purge_documents_outside(dirs)
        if purged:
            print(f"Removed {purged} documents from de-selected folders.")

        _start_indexing(embedding_engine, dirs)

    return {"monitored_dirs": dirs}


@app.get("/api/model")
def get_model():
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
    global embedding_engine

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

    with worker_lock:
        # Stop indexing before swapping the engine and wiping the index.
        _stop_indexing()

        # Persist the choice (as an explicit user choice), wipe the now-incompatible
        # index, recreate the vec table sized for the new model, and re-index.
        set_active_model_key(req.model_key, user_set=True)
        clear_index(new_engine.dim)
        set_index_model(req.model_key)
        embedding_engine = new_engine

        _start_indexing(embedding_engine, get_monitored_dirs())

    return {"active": req.model_key, "changed": True}


@app.post("/api/index/scan")
def trigger_rescan():
    # Snapshot: a settings/model change may swap the worker concurrently.
    w = worker
    if w is not None:
        w.scan_and_sync()
    return {"ok": True}


@app.post("/api/open-file")
def open_file(path: str):
    # Only launch files we actually indexed, so a caller that gets past the
    # Host/Origin guard (e.g. another local process) cannot os.startfile an
    # arbitrary path such as an executable.
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
    _start_parent_watchdog()
    uvicorn.run(app, host=API_HOST, port=API_PORT)
