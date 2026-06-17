import os
import threading
import numpy as np
import onnxruntime as ort
import requests
from tokenizers import Tokenizer
from huggingface_hub import hf_hub_download, hf_hub_url, get_hf_file_metadata
from src.config import MODEL_DIR, DEFAULT_MODEL_KEY, get_model_config


def _download_with_progress(repo: str, filename: str, dest_path, progress_cb):
    """Streams a Hub file to dest_path, reporting (downloaded, total) bytes.

    Writes to a .part file and atomically renames on success, so an interrupted
    download never leaves a truncated file that would be skipped on retry.
    """
    url = hf_hub_url(repo_id=repo, filename=filename)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(dest_path.name + ".part")

    # Resolve the true (uncompressed) file size up front. The streaming response
    # may omit Content-Length (e.g. for gzip-served, non-LFS files), so rely on
    # the Hub metadata for a reliable progress denominator; fall back to 0.
    try:
        total = get_hf_file_metadata(url).size or 0
    except Exception:
        total = 0

    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        if not total:
            total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        if progress_cb:
            progress_cb(downloaded, total)
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)

    os.replace(tmp_path, dest_path)


class EmbeddingEngine:
    def __init__(self, model_key: str = DEFAULT_MODEL_KEY, progress_callback=None):
        self.model_key = model_key
        self.cfg = get_model_config(model_key)
        self.dim = self.cfg["dim"]
        self.query_prefix = self.cfg["query_prefix"]
        self.passage_prefix = self.cfg["passage_prefix"]
        # Optional callback(model_key, filename, downloaded_bytes, total_bytes)
        # invoked during downloads so the UI can render a progress bar.
        self.progress_callback = progress_callback

        self.model_path, self.tokenizer_path = self._ensure_model_files()

        # Load the ONNX model session
        self.session = ort.InferenceSession(self.model_path, providers=['CPUExecutionProvider'])

        # Load the tokenizer
        self.tokenizer = Tokenizer.from_file(self.tokenizer_path)

        # Configure tokenizer options once (padding and truncation).
        # Doing this here instead of per-call avoids mutating shared tokenizer
        # state from multiple threads.
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self.tokenizer.enable_truncation(max_length=512)

        # Get expected input names of the ONNX model
        self.expected_inputs = {inp.name for inp in self.session.get_inputs()}

        # The engine is shared between the search handler (uvicorn event loop
        # thread) and the background IndexingWorker (QThread). Serialize access
        # so concurrent tokenizer/ONNX calls do not corrupt results or crash.
        self._lock = threading.Lock()

    def _ensure_model_files(self):
        """Downloads the ONNX model and tokenizer.json if not present locally.

        Each model is cached under its own subdirectory so different models do
        not collide on the shared onnx/model.onnx and tokenizer.json names.
        """
        model_root = MODEL_DIR / self.model_key
        onnx_file = self.cfg["onnx_file"]
        tokenizer_file = self.cfg["tokenizer_file"]

        model_onnx_path = model_root / onnx_file
        tokenizer_json_path = model_root / tokenizer_file

        # Ensure target directories exist (onnx_file may contain a subdir).
        model_onnx_path.parent.mkdir(parents=True, exist_ok=True)
        tokenizer_json_path.parent.mkdir(parents=True, exist_ok=True)

        repo = self.cfg["repo"]

        if not model_onnx_path.exists():
            print(f"Downloading ONNX model '{self.model_key}' from {repo}...")
            self._fetch(repo, onnx_file, model_root, model_onnx_path)

        if not tokenizer_json_path.exists():
            print(f"Downloading tokenizer for '{self.model_key}' from {repo}...")
            self._fetch(repo, tokenizer_file, model_root, tokenizer_json_path)

        return str(model_onnx_path), str(tokenizer_json_path)

    def _fetch(self, repo: str, filename: str, model_root, dest_path):
        """Downloads one model file, reporting progress when a callback is set.

        With a callback we stream the file ourselves to surface progress to the
        UI; otherwise we fall back to hf_hub_download (console progress, caching,
        resume) for the simple/offline path.
        """
        if self.progress_callback is not None:
            _download_with_progress(
                repo,
                filename,
                dest_path,
                lambda done, total: self.progress_callback(self.model_key, filename, done, total),
            )
        else:
            hf_hub_download(repo_id=repo, filename=filename, local_dir=str(model_root))

    def get_embeddings(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        if not texts:
            return []

        # Apply the model's asymmetric prefix (query vs passage). No-op for
        # symmetric models where both prefixes are empty.
        prefix = self.query_prefix if is_query else self.passage_prefix
        if prefix:
            texts = [prefix + t for t in texts]

        # Serialize tokenization + inference across threads.
        with self._lock:
            # Encode batch
            encodings = self.tokenizer.encode_batch(texts)

            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            token_type_ids = np.array([e.type_ids for e in encodings], dtype=np.int64)

            # Prepare inputs dynamically based on what the ONNX model expects
            inputs = {}
            if "input_ids" in self.expected_inputs:
                inputs["input_ids"] = input_ids
            if "attention_mask" in self.expected_inputs:
                inputs["attention_mask"] = attention_mask
            if "token_type_ids" in self.expected_inputs:
                inputs["token_type_ids"] = token_type_ids

            # Run inference
            outputs = self.session.run(None, inputs)

        token_embeddings = outputs[0]  # Shape: [batch_size, seq_len, 384]

        # Perform Mean Pooling
        input_mask_expanded = np.expand_dims(attention_mask, -1).astype(float)
        sum_embeddings = np.sum(token_embeddings * input_mask_expanded, axis=1)
        sum_mask = np.sum(input_mask_expanded, axis=1)
        sum_mask = np.clip(sum_mask, a_min=1e-9, a_max=None)
        embeddings = sum_embeddings / sum_mask

        # L2 Normalize embeddings
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        normalized_embeddings = embeddings / norms

        return normalized_embeddings.tolist()

    def get_embedding(self, text: str, is_query: bool = False) -> list[float]:
        return self.get_embeddings([text], is_query=is_query)[0]
