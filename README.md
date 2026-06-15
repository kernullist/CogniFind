# ContextFinder

ContextFinder is a 100% offline, on-device local semantic document search utility for Windows. It runs in the system tray, monitors designated directories in real-time, and allows you to instantly search through file contents using natural language queries via a Spotlight-style popup window.

---

## Features

- **Zero-Cloud Privacy**: All vector embeddings and SQLite databases are calculated and stored locally on your machine.
- **Low-Resource Background Indexing**: Automatically monitors system idle state using the Windows API. When active user input (mouse/keyboard) is detected, background indexing throttles its CPU usage to keep the operating system highly responsive.
- **Real-Time Watchdog**: Automatically detects file additions, modifications, renames, and deletions. Uses a debounced event queue to prevent redundant indexing during active file saves.
- **Spotlight-Style Search Popup**: A borderless, semi-transparent dark-themed overlay that is activated via a global system hotkey (Win + Alt + F) or by clicking the tray icon.
- **Supported Formats**: Parses and indexes Text/Markdown (.txt, .md), PDF (.pdf), Word (.docx), and Excel (.xlsx) files.
- **Hybrid Metadata Filtering**: Combines semantic vector similarity search with file metadata filters.

---

## Prerequisites

- **Operating System**: Windows
- **Python**: Version 3.10 or higher (Tested on 3.12.2)

---

## Installation

Install the required Python packages using pip:

```bash
pip install PySide6 watchdog pypdf python-docx openpyxl onnxruntime sqlite-vec huggingface_hub numpy
```

---

## How it Works

1. **Pre-heating**: On startup, the application pre-heats the local embedding engine. It downloads the all-MiniLM-L6-v2 ONNX model and tokenizer from the Hugging Face Hub if they are not already cached locally in your home directory (~/.cognifind/models/).
2. **Directory Scanning**: Scans watched directories (configured in SQLite/settings, defaulting to your Documents directory and a local test_watch folder). It deletes records of removed files and queues new or modified files.
3. **Chunking & Embeddings**: Extracts text from documents, splits it into 500-character chunks with a 50-character overlap, converts them into 384-dimensional normalized vectors via ONNX Runtime, and saves them in the local database (~/.cognifind/contextfinder.db).
4. **Vector Searching**: When you type a query, it is embedded into a vector, and a K-Nearest Neighbors (KNN) search is executed on the sqlite-vec virtual table using cosine distance.

---

## Project Structure

- **main.py**: The entry point that orchestrates the database initialization, embedding engine load, watcher services, and Qt application loop.
- **src/config.py**: Holds global constants, directory paths, and supported file extensions.
- **src/database.py**: Manages SQLite connection, loading the sqlite-vec extension, database schemas, metadata inserts, and similarity query execution.
- **src/embedding.py**: Downloads model files and runs ONNX inference with mean pooling and L2 normalization on CPU.
- **src/parser.py**: Dispatches file extensions to their respective extraction libraries (pypdf, python-docx, openpyxl) and splits text into chunks.
- **src/watcher.py**: Implements the watchdog event handler, debounces events, and indexes files in a background QThread with system idle throttling.
- **src/ui.py**: Implements the PySide6 borderless popup window, list view results with custom item delegates, system tray icon, and global hotkey listener.

---

## Usage

1. **Start the Application**:
   Run the orchestrator:
   ```bash
   python main.py
   ```
   A magnifying glass icon will appear in your Windows system tray, and the background thread will begin scanning your watched directories.

2. **Trigger the Search Bar**:
   Press **Win + Alt + F** from anywhere on your system to toggle the borderless search pop-up.

3. **Navigate & Open**:
   Type your natural language query. Results will update in real-time. Use the **Up/Down Arrow Keys** to navigate and press **Enter** (or double-click) to open the selected document with its default Windows application. Press **Escape** or click outside the window to dismiss the search bar.
