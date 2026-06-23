import os
import time
import ctypes
from ctypes import wintypes
from pathlib import Path
from threading import Lock
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from PySide6.QtCore import QThread

from src.config import (
    SUPPORTED_EXTENSIONS,
    DEBOUNCE_DELAY_SEC,
    MAX_FILE_SIZE_BYTES,
    IGNORED_DIR_NAMES,
    is_ignored_path,
    THROTTLE_IDLE_THRESHOLD_SEC,
    THROTTLE_ACTIVE_SEC,
    THROTTLE_IDLE_SEC,
    EMBED_BATCH_SIZE,
)
from src.database import (
    get_db_connection,
    init_db,
    get_file_hash_id,
    hash_text,
    upsert_document,
    update_document_metadata,
    get_document_index_state,
    insert_chunk,
    insert_embedding,
    delete_chunks,
    delete_document_by_path,
    get_all_indexed_files
)
from src.parser import extract_text, chunk_text

# Windows API structure for user idle detection
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.UINT),
        ('dwTime', wintypes.DWORD),
    ]

def get_idle_duration() -> float:
    """Returns the duration of system idle time in seconds using Windows API."""
    last_input_info = LASTINPUTINFO()
    last_input_info.cbSize = ctypes.sizeof(last_input_info)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last_input_info)):
        current_tick = ctypes.windll.kernel32.GetTickCount()
        elapsed_millis = (current_tick - last_input_info.dwTime) & 0xFFFFFFFF
        return elapsed_millis / 1000.0
    return 0.0

class IndexingWorker(QThread):
    def __init__(self, embedding_engine, monitored_dirs):
        super().__init__()
        self.embedding_engine = embedding_engine
        self.monitored_dirs = [Path(d) for d in monitored_dirs]
        self.is_running = True
        self.lock = Lock()

        # Current human-readable status, read directly by the API. (Qt signals
        # are not used: there is no Qt event loop in the host process, so a
        # cross-thread signal would never be delivered.)
        self.current_status = "starting"
        
        # Files that need indexing
        self.queue = []
        # Debouncing map: path -> last event time
        self.debounce_queue = {}
        
        # Files currently in database (loaded on startup)
        self.db_files = {}
        
        # Incremental scanner state
        self.scanning_complete = False
        self.walker = None

    def disk_file_generator(self):
        """Yields supported file paths from monitored directories one by one."""
        for directory in self.monitored_dirs:
            if not directory.exists():
                continue
            for root, dirs, files in os.walk(directory):
                # Prune ignored subdirectories in-place so os.walk does not descend
                # into them (build output, node_modules, .git, etc.).
                dirs[:] = [d for d in dirs if d.lower() not in IGNORED_DIR_NAMES]
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in SUPPORTED_EXTENSIONS:
                        yield os.path.join(root, file).replace("\\", "/")

    def purge_deleted_files(self):
        """Quickly checks if indexed files exist on disk, deletes them if they do not."""
        self.current_status = "cleaning database index..."
        self.db_files = get_all_indexed_files()
        deleted_count = 0
        for filepath_str in list(self.db_files.keys()):
            if not self.is_running:
                break
            # Remove files that no longer exist, or that now fall under an ignored
            # directory (e.g. after enabling/expanding the ignore rules).
            if not os.path.exists(filepath_str) or is_ignored_path(filepath_str):
                delete_document_by_path(filepath_str)
                deleted_count += 1

        if deleted_count > 0:
            print(f"Purged {deleted_count} missing/ignored files from DB index.")
            self.db_files = get_all_indexed_files()

    def run(self):
        # Ensure database is initialized
        init_db()
        
        # Pre-initialize scanner state
        self.purge_deleted_files()
        self.walker = self.disk_file_generator()
        self.scanning_complete = False
        
        self.current_status = "ready to scan"
        
        # Main event processing loop
        while self.is_running:
            # 1. Handle debounced files (priority!)
            now = time.time()
            files_to_index = []
            with self.lock:
                for path_str, event_time in list(self.debounce_queue.items()):
                    if now - event_time >= DEBOUNCE_DELAY_SEC:
                        files_to_index.append(path_str)
                        del self.debounce_queue[path_str]
            
            if files_to_index:
                with self.lock:
                    for f in files_to_index:
                        if f not in self.queue:
                            self.queue.append(f)
            
            # 2. If queue is empty and we are still scanning, advance directory walk
            if not self.queue and not self.scanning_complete:
                self.current_status = "scanning directories..."
                self.scan_next_batch(batch_size=50)
            
            # 3. Process the queue
            if self.queue:
                with self.lock:
                    path_str = self.queue.pop(0)
                
                filename = os.path.basename(path_str)
                self.current_status = f"indexing: {filename}"
                
                try:
                    self.index_file(path_str)
                except Exception as e:
                    print(f"Error indexing {path_str}: {e}")
            else:
                if self.scanning_complete:
                    self.current_status = "idle"
                time.sleep(0.1)

    def scan_next_batch(self, batch_size=50):
        """Scans the next N files from the generator and queues them if changed."""
        count = 0
        while count < batch_size and self.is_running:
            try:
                filepath_str = next(self.walker)
            except StopIteration:
                self.scanning_complete = True
                print("Directory scanning completed.")
                self.current_status = "scan completed"
                break
                
            count += 1
            
            try:
                path = Path(filepath_str)
                stat = path.stat()
                if stat.st_size > MAX_FILE_SIZE_BYTES:
                    continue
                    
                last_mod = datetime.fromtimestamp(stat.st_mtime).isoformat()
                
                if filepath_str not in self.db_files:
                    with self.lock:
                        if filepath_str not in self.queue:
                            self.queue.append(filepath_str)
                else:
                    db_meta = self.db_files[filepath_str]
                    if (db_meta['file_size'] != stat.st_size or 
                            db_meta['last_modified'] != last_mod):
                        with self.lock:
                            if filepath_str not in self.queue:
                                self.queue.append(filepath_str)
            except Exception as e:
                print(f"Error checking stat during scan for {filepath_str}: {e}")
                
        time.sleep(0.01)

    def scan_and_sync(self):
        """Triggers a manual re-scan by resetting the generator state."""
        with self.lock:
            self.queue.clear()
            self.debounce_queue.clear()
            self.scanning_complete = False
            
        self.purge_deleted_files()
        self.walker = self.disk_file_generator()
        print("Manual scan reset triggered.")

    def index_file(self, filepath_str: str):
        """Extracts text, chunks it, generates embeddings with throttling, and updates DB."""
        path = Path(filepath_str)
        if not path.exists():
            # If file was deleted while in queue, remove from DB
            delete_document_by_path(filepath_str)
            return
            
        stat = path.stat()
        file_size = stat.st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            print(f"Skipping {filepath_str}: File size ({file_size} bytes) exceeds limit ({MAX_FILE_SIZE_BYTES} bytes).")
            delete_document_by_path(filepath_str)
            return
            
        last_modified = stat.st_mtime
        doc_id = get_file_hash_id(filepath_str)

        # 1. Extract text, chunk it, and hash each chunk plus the whole document.
        text = extract_text(filepath_str)
        chunks = chunk_text(text) if text.strip() else []
        if not chunks:
            print(f"No indexable text extracted from {filepath_str} (empty or scanned without OCR).")
        chunk_hashes = [hash_text(c) for c in chunks]
        # Document content hash derived from the ordered chunk hashes.
        content_hash = hash_text("\x1f".join(chunk_hashes))

        # 2. Incremental: if the content is unchanged, only refresh metadata.
        #    This skips all embedding for the common "modified event but content
        #    did not actually change" case (editor rewrites, touch, etc.).
        existing = get_document_index_state(filepath_str)
        if existing is not None and existing[0] == content_hash:
            update_document_metadata(filepath_str, file_size, last_modified)
            return

        existing_chunks = existing[1] if existing is not None else {}

        # 3. Determine which chunk positions are new or changed (need embedding).
        to_embed = []
        for idx, h in enumerate(chunk_hashes):
            old = existing_chunks.get(idx)
            if old is None or old[1] != h:
                to_embed.append(idx)

        # 4-5. Embed the changed chunks and STREAM them to the DB in batches, so
        #    a huge document never holds tens of thousands of embeddings in memory
        #    at once (the dominant memory cost when indexing e.g. the Intel SDM).
        #    The document's content_hash is written only after every chunk is
        #    persisted, so an interrupted index leaves the document marked stale
        #    (hash NULL) and is simply resumed on the next scan rather than left
        #    half-done. Embedding happens between transactions, never inside one,
        #    so the slow/throttled work does not hold the write lock.
        changed = set(to_embed)
        conn = get_db_connection()
        try:
            # Create/refresh the document row but mark its content as NOT current
            # (content_hash=None), and drop chunks that were removed or changed.
            with conn:
                upsert_document(conn, filepath_str, path.name, path.suffix, file_size, last_modified, None)
                remove_ids = [
                    cid for idx, (cid, _h) in existing_chunks.items()
                    if idx >= len(chunks) or idx in changed
                ]
                delete_chunks(conn, remove_ids)

            # Embed in batches: one padded ONNX inference per batch is far faster
            # than one call per chunk, and the throttle is paid per batch rather
            # than per chunk (decisive for large docs). Each batch is persisted in
            # its own short transaction. Interruption is checked at every batch.
            for start in range(0, len(to_embed), EMBED_BATCH_SIZE):
                self.throttle_cpu()
                if not self.is_running:
                    raise InterruptedError("Indexing worker was stopped by user.")
                batch_idx = to_embed[start:start + EMBED_BATCH_SIZE]
                batch_vecs = self.embedding_engine.get_embeddings([chunks[i] for i in batch_idx])
                with conn:
                    for i, vec in zip(batch_idx, batch_vecs):
                        chunk_id = insert_chunk(conn, doc_id, i, chunks[i], chunk_hashes[i])
                        insert_embedding(conn, chunk_id, vec)

            # All chunks persisted -> mark the document's content as current.
            with conn:
                upsert_document(conn, filepath_str, path.name, path.suffix, file_size, last_modified, content_hash)
        except InterruptedError:
            print(f"Indexing interrupted for {filepath_str}. Will resume on next scan.")
        except Exception as e:
            print(f"Error indexing {filepath_str}: {e}")
        finally:
            conn.close()

    def throttle_cpu(self):
        """Sleeps to throttle CPU usage based on user idle state."""
        idle_seconds = get_idle_duration()
        if idle_seconds < THROTTLE_IDLE_THRESHOLD_SEC:
            # User active: stay gentle but responsive.
            time.sleep(THROTTLE_ACTIVE_SEC)
        else:
            # User idle: index quickly without pegging the CPU at 100%.
            time.sleep(THROTTLE_IDLE_SEC)

    def get_index_progress(self) -> dict:
        """Returns the current queue depth and whether the initial scan is ongoing."""
        with self.lock:
            queued = len(self.queue) + len(self.debounce_queue)
        return {"queued": queued, "scanning": not self.scanning_complete}

    def queue_file_for_indexing(self, filepath_str: str):
        """Queues file for debounced indexing."""
        filepath_str = filepath_str.replace("\\", "/")
        # Skip files under ignored directories (build output, deps, VCS, etc.).
        if is_ignored_path(filepath_str):
            return
        with self.lock:
            self.debounce_queue[filepath_str] = time.time()

    def stop(self):
        self.is_running = False
        self.wait()


class FileWatchHandler(FileSystemEventHandler):
    def __init__(self, worker):
        self.worker = worker

    def on_created(self, event):
        if not event.is_directory:
            ext = os.path.splitext(event.src_path)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                self.worker.queue_file_for_indexing(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            ext = os.path.splitext(event.src_path)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                self.worker.queue_file_for_indexing(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            ext = os.path.splitext(event.src_path)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                # Direct delete (no debouncing needed for deletes)
                filepath_str = event.src_path.replace("\\", "/")
                delete_document_by_path(filepath_str)

    def on_moved(self, event):
        if not event.is_directory:
            ext_src = os.path.splitext(event.src_path)[1].lower()
            ext_dest = os.path.splitext(event.dest_path)[1].lower()
            
            # Delete old path
            if ext_src in SUPPORTED_EXTENSIONS:
                filepath_str = event.src_path.replace("\\", "/")
                delete_document_by_path(filepath_str)
                
            # Queue new path
            if ext_dest in SUPPORTED_EXTENSIONS:
                self.worker.queue_file_for_indexing(event.dest_path)


class WatcherManager:
    def __init__(self, monitored_dirs, worker):
        self.monitored_dirs = monitored_dirs
        self.worker = worker
        self.observer = Observer()
        self.handler = FileWatchHandler(self.worker)

    def start(self):
        for directory in self.monitored_dirs:
            if os.path.exists(directory):
                self.observer.schedule(self.handler, directory, recursive=True)
        self.observer.start()

    def stop(self):
        self.observer.stop()
        self.observer.join()
