This directory is populated at build time by scripts/fetch_models.py
(invoked from build.ps1) with the embedding models bundled for offline use:

    <model_key>/onnx/model.onnx
    <model_key>/tokenizer.json

The model binaries are intentionally not committed to git. This placeholder
keeps the directory present so the Tauri resource bundling step always has a
valid path. At runtime the Tauri shell passes COGNIFIND_MODELS_DIR pointing at
this folder inside the installed app's resource directory.
