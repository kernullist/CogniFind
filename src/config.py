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

# Hybrid search: how much a lexical (keyword/substring) match boosts a result's
# semantic score. final = semantic_similarity + weight * lexical_fraction. Helps
# short/acronym/exact-term queries (e.g. "dma") where pure dense search is weak.
HYBRID_KEYWORD_WEIGHT = 0.3

# Hard upper bound on the k value in a sqlite-vec KNN query. vec0 rejects any
# larger k with "k value in knn query too large" (the built-in limit is 4096).
# Metadata-filtered searches must clamp their candidate pool to this.
VEC_MAX_K = 4096

def get_model_config(key: str) -> dict:
    """Returns the registry entry for a model key, falling back to the default."""
    return EMBEDDING_MODELS.get(key, EMBEDDING_MODELS[DEFAULT_MODEL_KEY])

def _is_korean_locale() -> bool:
    """True if the Windows UI/regional language is Korean."""
    try:
        import ctypes
        # LANG_KOREAN == 0x12; primary language is the low 10 bits of the LANGID.
        for fn in ("GetUserDefaultUILanguage", "GetUserDefaultLangID"):
            langid = getattr(ctypes.windll.kernel32, fn)()
            if (langid & 0x3FF) == 0x12:
                return True
    except Exception:
        pass
    try:
        import locale
        return (locale.getdefaultlocale()[0] or "").lower().startswith("ko")
    except Exception:
        return False

def get_default_model_key() -> str:
    """First-run default model: the multilingual model on a Korean system, the
    fast English model otherwise. Only used when the index is still empty."""
    if "e5-multilingual" in EMBEDDING_MODELS and _is_korean_locale():
        return "e5-multilingual"
    return DEFAULT_MODEL_KEY

# Chunking settings
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
# Cap chunks per document so a single huge file (e.g. a multi-MB log/export)
# cannot monopolize the indexer for a very long time. ~2000 chunks covers the
# first ~900KB of text, which is plenty for a document to be findable.
MAX_CHUNKS_PER_DOC = 2000

# Watcher settings
DEBOUNCE_DELAY_SEC = 1.0

# CPU throttle: seconds slept per chunk during indexing, by activity state.
# Embedding a chunk is brief (~10ms), so even the "active" value stays gentle
# (~15% duty cycle) while keeping indexing responsive.
THROTTLE_IDLE_THRESHOLD_SEC = 5.0   # idle longer than this -> index fast
THROTTLE_ACTIVE_SEC = 0.05          # machine in active use
THROTTLE_IDLE_SEC = 0.01            # machine idle

# Supported file extensions
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx"}

# Directory names skipped during indexing: build artifacts, VCS, dependencies,
# and caches. Matched case-insensitively against each path component. This keeps
# generated junk (e.g. node_modules, build output) out of the index, which
# matters once broader folders are watched.
IGNORED_DIR_NAMES = frozenset({
    ".git", ".svn", ".hg",
    "node_modules", "bower_components", "vendor",
    ".venv", "venv", "__pycache__",
    ".gradle", ".idea", ".vs", ".vscode", ".cache", ".next", ".nuxt",
    "build", "dist", "out", "target", "bin", "obj", "intermediates",
    "$recycle.bin", "system volume information",
})

def is_ignored_path(path_str: str) -> bool:
    """Returns True if any component of the path is an ignored directory name."""
    parts = path_str.replace("\\", "/").lower().split("/")
    return any(part in IGNORED_DIR_NAMES for part in parts)

# Maximum file size to index (10 MB)
MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

def get_default_watch_dirs():
    """Returns the default watch directories: the user's common document folders."""
    home = Path(os.path.expanduser("~"))
    dirs = []
    for sub in ("Documents", "Desktop", "Downloads", "OneDrive"):
        p = home / sub
        if p.exists():
            dirs.append(str(p).replace("\\", "/"))
    # test_watch is a dev-only convenience. In the frozen app __file__ lives in a
    # temporary PyInstaller extraction dir, so creating/watching it is pointless.
    if not FROZEN:
        test_watch = Path(__file__).resolve().parent.parent / "test_watch"
        test_watch.mkdir(parents=True, exist_ok=True)
        dirs.append(str(test_watch).replace("\\", "/"))
    return dirs
