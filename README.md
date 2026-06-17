# ContextFinder

ContextFinder is a 100% offline, on-device local semantic document search utility for Windows. It runs in the system tray, monitors designated directories in real-time, and allows you to instantly search through file contents using natural language queries via a Spotlight-style popup window.

---

## Features

- **Zero-Cloud Privacy**: All vector embeddings and SQLite databases are calculated and stored locally on your machine.
- **Low-Resource Background Indexing**: Automatically monitors system idle state using the Windows API. When active user input (mouse/keyboard) is detected, background indexing throttles its CPU usage to keep the operating system highly responsive.
- **Real-Time Watchdog**: Automatically detects file additions, modifications, renames, and deletions. Uses a debounced event queue to prevent redundant indexing during active file saves.
- **Spotlight-Style Search Popup**: A borderless, semi-transparent dark-themed overlay that is activated via a global system hotkey (Win + Alt + F) or by clicking the tray icon.
- **Supported Formats**: Parses and indexes Text/Markdown (.txt, .md), PDF (.pdf), Word (.docx), and Excel (.xlsx) files.
- **Hybrid Metadata Filtering**: Combines semantic vector similarity search with file metadata filters (file type, date range).
- **Modern UI**: Built with Tauri + React + TypeScript for a fast, responsive user experience.

---

## Prerequisites

### For Development

- **Operating System**: Windows 10/11
- **Python**: Version 3.10 or higher (Tested on 3.12.2)
- **Node.js**: Version 18 or higher (Tested on 24.14.1)
- **Rust**: Version 1.77 or higher (Tested on 1.96.0)

### For Production Use

- **Operating System**: Windows 10/11
- No additional dependencies required (all bundled in installer)

---

## Installation

### Production Build (Recommended)

Download and run the installer from the releases page:

- `CogniFind_0.1.0_x64-setup.exe` (NSIS installer)
- `CogniFind_0.1.0_x64_en-US.msi` (MSI installer)

### Development Setup

1. **Install Python dependencies**:

```bash
pip install PySide6 watchdog pypdf python-docx openpyxl onnxruntime sqlite-vec huggingface_hub numpy fastapi uvicorn pyinstaller
```

2. **Install Tauri CLI**:

```bash
npm install -g @tauri-apps/cli@latest
```

3. **Install frontend dependencies**:

```bash
cd frontend
npm install
```

---

## How it Works

1. **Pre-heating**: On startup, the Python backend pre-heats the local embedding engine. It downloads the all-MiniLM-L6-v2 ONNX model and tokenizer from the Hugging Face Hub if they are not already cached locally in your home directory (~/.cognifind/models/).
2. **Directory Scanning**: Scans watched directories (configured in SQLite/settings, defaulting to your Documents directory and a local test_watch folder). It deletes records of removed files and queues new or modified files.
3. **Chunking & Embeddings**: Extracts text from documents, splits it into 500-character chunks with a 50-character overlap, converts them into 384-dimensional normalized vectors via ONNX Runtime, and saves them in the local database (~/.cognifind/contextfinder.db).
4. **Vector Searching**: When you type a query, it is embedded into a vector, and a K-Nearest Neighbors (KNN) search is executed on the sqlite-vec virtual table using cosine distance.

---

## Architecture

ContextFinder uses a **dual-process architecture**:

```
┌─────────────────────────────────┐
│   Tauri Frontend (Rust + React) │
│  - Frameless transparent window │
│  - Global hotkey (Alt+Super+F)  │
│  - System tray icon             │
│  - Python process management    │
└──────────────┬──────────────────┘
               │ HTTP (localhost:8765)
               ▼
┌─────────────────────────────────┐
│   Python Backend (FastAPI)      │
│  - ONNX embedding engine        │
│  - SQLite + sqlite-vec          │
│  - watchdog file monitoring     │
│  - Text parsing & chunking      │
└─────────────────────────────────┘
```

The Tauri frontend handles UI rendering and system integration, while the Python backend performs all AI/ML operations and database management.

---

## Project Structure

```
CogniFind/
├── api.py                          # FastAPI backend server (localhost:8765)
├── build.ps1                       # Production build script
├── dev.ps1                         # Development mode script
├── cognifind-backend.spec          # PyInstaller configuration
├── src/                            # Python modules
│   ├── config.py                   # Global constants and paths
│   ├── database.py                 # SQLite + sqlite-vec operations
│   ├── embedding.py                # ONNX model inference
│   ├── parser.py                   # Document text extraction
│   └── watcher.py                  # File system monitoring
├── frontend/                       # Tauri application
│   ├── src/
│   │   ├── App.tsx                 # React UI components
│   │   ├── App.css                 # Dark theme styles
│   │   ├── api.ts                  # Python API client
│   │   └── types.ts                # TypeScript type definitions
│   ├── src-tauri/
│   │   ├── src/lib.rs              # Rust: process management, hotkey, tray
│   │   ├── Cargo.toml              # Rust dependencies
│   │   └── tauri.conf.json         # Tauri configuration
│   └── package.json                # Node.js dependencies
└── main.py                         # Legacy PySide6 entry point (deprecated)
```

---

## Usage

### Production

1. **Install the application** using the provided installer.
2. **Launch ContextFinder** from the Start Menu or desktop shortcut.
3. A magnifying glass icon will appear in your Windows system tray, and the background thread will begin scanning your watched directories.

### Development

```bash
# Option 1: Use the dev script
.\dev.ps1

# Option 2: Manual start
cd frontend
npx tauri dev
```

### Using the Search

1. **Trigger the Search Bar**: Press **Win + Alt + F** from anywhere on your system to toggle the borderless search pop-up.

2. **Navigate & Open**: Type your natural language query. Results will update in real-time. Use the **Up/Down Arrow Keys** to navigate and press **Enter** (or double-click) to open the selected document with its default Windows application. Press **Escape** or click outside the window to dismiss the search bar.

3. **Filter Results**: Use the dropdown filters to narrow results by date range (Today, This Week, This Month) or file type (PDF, DOCX, XLSX, TXT, MD).

---

## Building from Source

Run the full build pipeline:

```powershell
.\build.ps1
```

This script will:

1. Build the Python backend with PyInstaller (~90MB)
2. Copy the backend executable to Tauri's binaries directory
3. Build the React frontend with Vite
4. Compile the Tauri application
5. Generate installers (NSIS and MSI)

Output files will be in `frontend/src-tauri/target/release/bundle/`.

---

## Configuration

### Monitored Directories

By default, ContextFinder monitors:

- `C:\Users\<username>\Documents`
- `C:\git\CogniFind\test_watch` (development only)

You can modify monitored directories through the settings API:

```bash
curl -X PUT http://localhost:8765/api/settings \
  -H "Content-Type: application/json" \
  -d '{"monitored_dirs": ["C:/Users/username/Documents", "D:/Projects"]}'
```

### Database Location

All data is stored in `~/.cognifind/`:

- `contextfinder.db` - SQLite database with documents and embeddings
- `models/` - ONNX model files (downloaded on first run)

---

## API Reference

The Python backend exposes a REST API on `http://localhost:8765`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/search` | POST | Semantic search with filters |
| `/api/status` | GET | Get indexing status |
| `/api/settings` | GET | Get monitored directories |
| `/api/settings` | PUT | Update monitored directories |
| `/api/model` | GET | Get active and available embedding models |
| `/api/model` | PUT | Switch embedding model (clears index and re-indexes) |
| `/api/index/scan` | POST | Trigger manual rescan |
| `/api/open-file` | POST | Open file with default application |

### Search Request Example

```json
{
  "query": "marketing report from last week",
  "date_from": "2024-01-01T00:00:00",
  "date_to": null,
  "extensions": [".pdf", ".docx"],
  "limit": 5
}
```

---

## License

This project is provided as-is for educational and personal use.
