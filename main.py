import sys
from PySide6.QtWidgets import QApplication
from src.embedding import EmbeddingEngine
from src.database import init_db, get_monitored_dirs
from src.watcher import IndexingWorker, WatcherManager
from src.ui import SearchWindow, SystemTrayApp

def main():
    # 1. Initialize SQLite Database Schema
    init_db()
    
    # 2. Create the Qt Application
    app = QApplication(sys.argv)
    
    # Critical: prevents app from exiting when the search window hides
    app.setQuitOnLastWindowClosed(False)
    
    # 3. Load / Pre-heat the ONNX Embedding Engine
    print("Pre-heating Embedding Engine...")
    embedding_engine = EmbeddingEngine()
    
    # 4. Create the Spotlight Search Window
    search_window = SearchWindow(embedding_engine)
    
    # 5. Fetch monitored folders from database settings
    monitored_dirs = get_monitored_dirs()
    print(f"Monitored directories: {monitored_dirs}")
    
    # 6. Initialize Background Index Worker Thread
    worker = IndexingWorker(embedding_engine, monitored_dirs)
    
    # 7. Initialize watchdog File System Watcher
    watcher = WatcherManager(monitored_dirs, worker)
    
    # 8. Assemble everything in the System Tray Application wrapper
    tray_app = SystemTrayApp(app, search_window, worker, watcher)
    tray_app.start()
    
    print("ContextFinder is running in the background. Press Win + Alt + F to search.")
    
    # Execute the Qt Event loop
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
