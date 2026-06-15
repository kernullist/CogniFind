import os
from pathlib import Path

# Base App Data Directory
APP_DATA_DIR = Path(os.path.expanduser("~")) / ".cognifind"
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Database path
DB_PATH = APP_DATA_DIR / "contextfinder.db"

# Model directory
MODEL_DIR = APP_DATA_DIR / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# Embedding settings
HF_REPO = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# Chunking settings
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

# Watcher settings
DEBOUNCE_DELAY_SEC = 1.0

# Supported file extensions
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx"}

# Maximum file size to index (10 MB)
MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

def get_default_watch_dirs():
    """Returns a list of default watch directories, focusing on Documents."""
    docs = Path(os.path.expanduser("~")) / "Documents"
    # Create a local test folder for safety/testing as well
    test_watch = Path("C:/git/CogniFind/test_watch")
    test_watch.mkdir(parents=True, exist_ok=True)
    
    dirs = []
    if docs.exists():
        dirs.append(str(docs).replace("\\", "/"))
    if test_watch.exists():
        dirs.append(str(test_watch).replace("\\", "/"))
    return dirs
