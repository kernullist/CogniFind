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
        # Handle tick overflow (occurs after 49.7 days)
        elapsed_millis = current_tick - last_input_info.dwTime
        if elapsed_millis < 0:
            # Simple fallback on overflow
            elapsed_millis = 0
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

    def run(self):
        # Ensure database is initialized
        init_db()
        
        # Perform initial scan
        self.status_changed.emit("scanning directories...")
        self.scan_and_sync()
        
        # Main event processing loop
        while self.is_running:
            # 1. Handle debounced files
            now = time.time()
            files_to_index = []
            with self.lock:
                for path_str, event_time in list(self.debounce_queue.items()):
                    if now - event_time >= DEBOUNCE_DELAY_SEC:
                        files_to_index.append(path_str)
                        del self.debounce_queue[path_str]
            
            # 2. Add to process queue
            if files_to_index:
                with self.lock:
                    for f in files_to_index:
                        if f not in self.queue:
                            self.queue.append(f)
            
            # 3. Process the queue
            if self.queue:
                total = len(self.queue)
                current = 0
                while self.queue and self.is_running:
                    # Pop from queue
                    with self.lock:
                        path_str = self.queue.pop(0)
                    
                    current += 1
                    filename = os.path.basename(path_str)
                    self.status_changed.emit(f"indexing: {filename}")
                    self.progress_changed.emit(current, total)
                    
                    try:
                        self.index_file(path_str)
                    except Exception as e:
                        print(f"Error indexing {path_str}: {e}")
                        
                self.status_changed.emit("idle")
                self.progress_changed.emit(0, 0)
                self.indexing_finished.emit()
            else:
                self.status_changed.emit("idle")
                time.sleep(0.5)

    def scan_and_sync(self):
        """Scans directories, deletes missing files from DB, and queues changed files."""
        self.db_files = get_all_indexed_files()
        disk_files = {}
        
        for directory in self.monitored_dirs:
            if not directory.exists():
                continue
            
            # Walk directory recursively
            for root, _, files in os.walk(directory):
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in SUPPORTED_EXTENSIONS:
                        filepath = Path(root) / file
                        filepath_str = str(filepath).replace("\\", "/")
                        try:
                            stat = filepath.stat()
                            if stat.st_size > MAX_FILE_SIZE_BYTES:
                                continue
                            disk_files[filepath_str] = {
                                'last_modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                                'file_size': stat.st_size
                            }
                        except Exception as e:
                            print(f"Error accessing stat for {filepath_str}: {e}")
                            
        # 1. Clean up deleted files from database
        deleted_count = 0
        for db_path in list(self.db_files.keys()):
            if db_path not in disk_files:
                delete_document_by_path(db_path)
                deleted_count += 1
                
        if deleted_count > 0:
            print(f"Deleted {deleted_count} missing files from DB index.")
            
        # 2. Detect new or modified files
        new_or_modified = []
        for disk_path, meta in disk_files.items():
            if disk_path not in self.db_files:
                new_or_modified.append(disk_path)
            else:
                db_meta = self.db_files[disk_path]
                # Compare modified timestamp and size
                # Handle potential timestamp formatting variations
                if (db_meta['file_size'] != meta['file_size'] or 
                        db_meta['last_modified'] != meta['last_modified']):
                    new_or_modified.append(disk_path)
                    
        # Add to queue
        with self.lock:
            self.queue.extend(new_or_modified)
            
        print(f"Initial scan: queued {len(new_or_modified)} files for indexing.")
        self.status_changed.emit(f"queued {len(new_or_modified)} files")

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
        
        # 1. Extract text
        text = extract_text(filepath_str)
        if not text.strip():
            # Empty file: store document metadata only (no chunks)
            conn = get_db_connection()
            insert_document(conn, filepath_str, path.name, path.suffix, file_size, last_modified)
            conn.commit()
            conn.close()
            return
            
        # 2. Chunk text
        chunks = chunk_text(text)
        if not chunks:
            return
            
        # 3. Generate embeddings & Insert (with Throttling)
        conn = get_db_connection()
        try:
            # Insert document and get ID
            doc_id = insert_document(conn, filepath_str, path.name, path.suffix, file_size, last_modified)
            
            # Process chunks in batches to throttle without locking database too long
            for idx, chunk in enumerate(chunks):
                # Throttle CPU: Check if user is active
                self.throttle_cpu()
                
                # Check if worker was stopped mid-process
                if not self.is_running:
                    break
                    
                # Generate embedding
                embedding = self.embedding_engine.get_embedding(chunk)
                
                # Insert chunk and embedding
                chunk_id = insert_chunk(conn, doc_id, idx, chunk)
                insert_embedding(conn, chunk_id, embedding)
                
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
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
