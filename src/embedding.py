import os
import threading
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
from huggingface_hub import hf_hub_download
from src.config import MODEL_DIR, HF_REPO

class EmbeddingEngine:
    def __init__(self):
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
        """Downloads the ONNX model and tokenizer.json if not present locally."""
        model_onnx_path = MODEL_DIR / "onnx" / "model.onnx"
        tokenizer_json_path = MODEL_DIR / "tokenizer.json"
        
        # Ensure target directories exist
        (MODEL_DIR / "onnx").mkdir(parents=True, exist_ok=True)
        
        if not model_onnx_path.exists():
            print("Downloading ONNX model file from Hugging Face Hub...")
            hf_hub_download(
                repo_id=HF_REPO,
                filename="onnx/model.onnx",
                local_dir=str(MODEL_DIR)
            )
            
        if not tokenizer_json_path.exists():
            print("Downloading tokenizer.json file from Hugging Face Hub...")
            hf_hub_download(
                repo_id=HF_REPO,
                filename="tokenizer.json",
                local_dir=str(MODEL_DIR)
            )
            
        return str(model_onnx_path), str(tokenizer_json_path)

    def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

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

    def get_embedding(self, text: str) -> list[float]:
        return self.get_embeddings([text])[0]
