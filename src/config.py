import os
import sys
from pathlib import Path

# Base App Data Directory
APP_DATA_DIR = Path(os.path.expanduser("~")) / ".cognifind"
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Database path
DB_PATH = APP_DATA_DIR / "contextfinder.db"

# Per-user model cache (downloaded models live here).
MODEL_DIR = APP_DATA_DIR / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# Bundled model location for offline deployment. The portable distribution keeps
# models in a "models" folder next to the executable so they persist across
# exe-only updates. Resolution: explicit COGNIFIND_MODELS_DIR, else (frozen) the
# folder next to the executable, else (dev) the project-root ./models populated
# by scripts/fetch_models.py.
FROZEN = getattr(sys, "frozen", False)
_env_models = os.environ.get("COGNIFIND_MODELS_DIR")
if _env_models:
    BUNDLED_MODELS_DIR = Path(_env_models)
elif FROZEN:
    BUNDLED_MODELS_DIR = Path(sys.executable).resolve().parent / "models"
else:
    BUNDLED_MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

# Whether the app may download models that are not bundled or cached. Disabled in
# the shipped (frozen) app for fully offline operation; overridable via env
# COGNIFIND_ALLOW_DOWNLOAD=1/0.
_env_dl = os.environ.get("COGNIFIND_ALLOW_DOWNLOAD")
if _env_dl is not None:
    ALLOW_MODEL_DOWNLOAD = _env_dl == "1"
else:
    ALLOW_MODEL_DOWNLOAD = not FROZEN

# Embedding model registry.
# Each model is downloaded as an ONNX file + tokenizer.json from the Hugging
# Face Hub. e5-style models require asymmetric prefixes ("query:" / "passage:")
# for best retrieval quality; symmetric models leave both empty.
EMBEDDING_MODELS = {
    "minilm": {
        "label": "all-MiniLM-L6-v2 (English, fast)",
        "repo": "sentence-transformers/all-MiniLM-L6-v2",
        "onnx_file": "onnx/model.onnx",
        "tokenizer_file": "tokenizer.json",
        "dim": 384,
        "query_prefix": "",
        "passage_prefix": "",
    },
    "e5-multilingual": {
        "label": "multilingual-e5-small (Korean / multilingual)",
        "repo": "Xenova/multilingual-e5-small",
        "onnx_file": "onnx/model.onnx",
        "tokenizer_file": "tokenizer.json",
        "dim": 384,
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    },
}

DEFAULT_MODEL_KEY = "minilm"

def get_model_config(key: str) -> dict:
    """Returns the registry entry for a model key, falling back to the default."""
    return EMBEDDING_MODELS.get(key, EMBEDDING_MODELS[DEFAULT_MODEL_KEY])

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
    project_root = Path(__file__).resolve().parent.parent
    test_watch = project_root / "test_watch"
    test_watch.mkdir(parents=True, exist_ok=True)
    
    dirs = []
    if docs.exists():
        dirs.append(str(docs).replace("\\", "/"))
    if test_watch.exists():
        dirs.append(str(test_watch).replace("\\", "/"))
    return dirs
