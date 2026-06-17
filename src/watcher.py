import os
import time
import ctypes
from ctypes import wintypes
from pathlib import Path
from threading import Lock
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from PySide6.QtCore import QThread, Signal

from src.config import SUPPORTED_EXTENSIONS, DEBOUNCE_DELAY_SEC, MAX_FILE_SIZE_BYTES
from src.database import (
    get_db_connection,
    init_db,
    insert_document,
    insert_chunk,
    insert_embedding,
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
    status_changed = Signal(str)
    progress_changed = Signal(int, int) # (current, total)
    indexing_finished = Signal()

    def __init__(self, embedding_engine, monitored_dirs):
        super().__init__()
        self.embedding_engine = embedding_engine
        self.monitored_dirs = [Path(d) for d in monitored_dirs]
        self.is_running = True
        self.lock = Lock()
        
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
            for root, _, files in os.walk(directory):
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in SUPPORTED_EXTENSIONS:
                        yield os.path.join(root, file).replace("\\", "/")

    def purge_deleted_files(self):
        """Quickly checks if indexed files exist on disk, deletes them if they do not."""
        self.status_changed.emit("cleaning database index...")
        self.db_files = get_all_indexed_files()
        deleted_count = 0
        for filepath_str in list(self.db_files.keys()):
            if not self.is_running:
                break
            if not os.path.exists(filepath_str):
                delete_document_by_path(filepath_str)
                deleted_count += 1
                
        if deleted_count > 0:
            print(f"Purged {deleted_count} missing files from DB index.")
            self.db_files = get_all_indexed_files()

    def run(self):
        # Ensure database is initialized
        init_db()
        
        # Pre-initialize scanner state
        self.purge_deleted_files()
        self.walker = self.disk_file_generator()
        self.scanning_complete = False
        
        self.status_changed.emit("ready to scan")
        
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
                self.status_changed.emit("scanning directories...")
                self.scan_next_batch(batch_size=50)
            
            # 3. Process the queue
            if self.queue:
                with self.lock:
                    path_str = self.queue.pop(0)
                
                filename = os.path.basename(path_str)
                self.status_changed.emit(f"indexing: {filename}")
                
                try:
                    self.index_file(path_str)
                except Exception as e:
                    print(f"Error indexing {path_str}: {e}")
            else:
                if self.scanning_complete:
                    self.status_changed.emit("idle")
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
                self.status_changed.emit("scan completed")
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

        # 1. Extract text and prepare chunks
        text = extract_text(filepath_str)
        chunks = chunk_text(text) if text.strip() else []

        # 2. Generate embeddings up front, OUTSIDE of any DB transaction.
        #    Embedding is slow and throttled (up to 250ms/chunk while the user
        #    is active). Doing it inside the write transaction would hold the
        #    SQLite write lock for the entire duration, starving concurrent
        #    writers (watcher deletes) until busy_timeout expires and blocking
        #    WAL checkpoints. Compute first, then write in one quick transaction.
        embeddings = []
        try:
            for chunk in chunks:
                # Throttle CPU: Check if user is active
                self.throttle_cpu()

                # If worker was stopped mid-process, abort before touching the DB
                if not self.is_running:
                    raise InterruptedError("Indexing worker was stopped by user.")

                embeddings.append(self.embedding_engine.get_embedding(chunk))
        except InterruptedError:
            print(f"Indexing interrupted for {filepath_str}. Nothing written.")
            return
        except Exception as e:
            print(f"Error embedding {filepath_str}: {e}")
            return

        # 3. Persist document, chunks and embeddings in a single atomic transaction.
        conn = get_db_connection()
        try:
            with conn:
                # Insert document and get ID (deletes old chunks if existing)
                doc_id = insert_document(conn, filepath_str, path.name, path.suffix, file_size, last_modified)

                for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                    chunk_id = insert_chunk(conn, doc_id, idx, chunk)
                    insert_embedding(conn, chunk_id, embedding)
        except Exception as e:
            print(f"Error indexing {filepath_str}: {e}")
        finally:
            conn.close()

    def throttle_cpu(self):
        """Sleeps to throttle CPU usage based on user idle state."""
        idle_seconds = get_idle_duration()
        if idle_seconds < 5.0:
            # User active: throttle heavily
            time.sleep(0.25) # Sleep 250ms per chunk
        else:
            # User idle: sleep minimally to prevent pegging CPU at 100%
            time.sleep(0.01)

    def queue_file_for_indexing(self, filepath_str: str):
        """Queues file for debounced indexing."""
        filepath_str = filepath_str.replace("\\", "/")
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
