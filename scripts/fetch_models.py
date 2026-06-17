"""Downloads all registered embedding models for offline bundling.

Usage:
    python scripts/fetch_models.py [TARGET_DIR] [model_key ...]

TARGET_DIR defaults to <project_root>/models. Optional model keys limit which
models are fetched (default: all in EMBEDDING_MODELS). Files are laid out as
<TARGET_DIR>/<model_key>/<onnx_file> and <tokenizer_file>, matching what the
backend expects via BUNDLED_MODELS_DIR. Already-present files are skipped, so
re-running is cheap and incremental.
"""
import sys
from pathlib import Path

# Allow importing the src package when run from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from huggingface_hub import hf_hub_download
from src.config import EMBEDDING_MODELS


def main():
    args = sys.argv[1:]
    target = Path(args[0]) if args else (PROJECT_ROOT / "models")
    requested = args[1:] if len(args) > 1 else list(EMBEDDING_MODELS.keys())

    for key in requested:
        cfg = EMBEDDING_MODELS.get(key)
        if cfg is None:
            print(f"[warn] unknown model key: {key}")
            continue

        dest = target / key
        dest.mkdir(parents=True, exist_ok=True)
        for filename in (cfg["onnx_file"], cfg["tokenizer_file"]):
            out_path = dest / filename
            if out_path.exists():
                print(f"[skip] {key}/{filename}")
                continue
            print(f"[get ] {key}/{filename}  <- {cfg['repo']}")
            hf_hub_download(repo_id=cfg["repo"], filename=filename, local_dir=str(dest))

    print(f"Models ready under: {target}")


if __name__ == "__main__":
    main()
