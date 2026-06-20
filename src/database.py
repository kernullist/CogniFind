import sqlite3
import sqlite_vec
import hashlib
import json
from pathlib import Path
from datetime import datetime
from src.config import DB_PATH, DEFAULT_MODEL_KEY, get_model_config, HYBRID_KEYWORD_WEIGHT

def hash_text(text: str) -> str:
    """Returns the SHA-256 hex digest of a text string (used for change detection)."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    return conn

def _create_embeddings_table(cursor, dim: int):
    """Creates the sqlite-vec virtual table sized for the given embedding dim.

    dim comes from the trusted model registry (an int), so the f-string is safe.
    """
    cursor.execute(f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_embeddings USING vec0(
        chunk_id INTEGER PRIMARY KEY,
        embedding float[{int(dim)}] distance_metric=cosine
    );
    """)

def _ensure_column(cursor, table: str, column: str, decl: str):
    """Adds a column to an existing table if it is not already present (migration)."""
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("PRAGMA foreign_keys = ON;")

    # 1. documents table (content_hash supports incremental re-indexing)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        file_path TEXT NOT NULL UNIQUE,
        file_name TEXT NOT NULL,
        file_extension TEXT NOT NULL,
        file_size INTEGER NOT NULL,
        last_modified TIMESTAMP NOT NULL,
        content_hash TEXT,
        indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 2. document_chunks table (chunk_hash supports per-chunk reuse)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS document_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        text_content TEXT NOT NULL,
        chunk_hash TEXT,
        FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
    );
    """)

    # 4. settings table (created before reading the active model below)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)

    # Migrate older databases that predate the hash columns.
    _ensure_column(cursor, "documents", "content_hash", "TEXT")
    _ensure_column(cursor, "document_chunks", "chunk_hash", "TEXT")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON document_chunks(document_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_documents_file_path ON documents(file_path);")

    # 3. chunk_embeddings virtual table, sized for the ACTIVE model's dimension
    #    (which may be the locale default, not the persisted embedding_model).
    active_key = _resolve_active_model(lambda k: _get_setting(cursor, k))
    _create_embeddings_table(cursor, get_model_config(active_key)["dim"])

    conn.commit()
    conn.close()

def _get_setting(cursor, key: str):
    """Reads a single settings value using an existing cursor; returns None if absent."""
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else None

def get_setting(key: str):
    """Reads a settings value (own connection); returns None if absent."""
    conn = get_db_connection()
    try:
        return _get_setting(conn.cursor(), key)
    finally:
        conn.close()

def set_setting(key: str, value: str):
    """Writes a settings value."""
    conn = get_db_connection()
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()

def _resolve_active_model(get_fn) -> str:
    """Active model: the user's explicit choice if set, else the locale-aware
    default. get_fn(key) reads a setting (cursor-based or own-connection), so
    this can run inside init_db's transaction or standalone."""
    from src.config import get_default_model_key
    if get_fn("model_user_set") == "1":
        saved = get_fn("embedding_model")
        if saved:
            return saved
    return get_default_model_key()

def get_active_model_key() -> str:
    """Returns the active embedding model: the user's explicit choice if they
    made one, otherwise the locale-aware default (the Korean model on a Korean
    system). Not persisted, so the default is re-evaluated each run until the
    user explicitly picks a model."""
    return _resolve_active_model(get_setting)

def set_active_model_key(key: str, user_set: bool = False):
    """Persists the active embedding model key. user_set=True marks it as an
    explicit user choice (which then overrides the locale default)."""
    set_setting("embedding_model", key)
    if user_set:
        set_setting("model_user_set", "1")

def get_index_model():
    """Returns the model the current index was built with, or None if unknown."""
    return get_setting("index_model")

def set_index_model(key: str):
    """Records the model the current index is built with."""
    set_setting("index_model", key)

def clear_index(new_dim: int):
    """Wipes all documents/chunks/embeddings and recreates the vec table.

    Used when switching embedding models: vectors from different models are not
    comparable, so a full re-index is required (and the dimension may change).
    """
    conn = get_db_connection()
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("DROP TABLE IF EXISTS chunk_embeddings")
            cursor.execute("DELETE FROM document_chunks")
            cursor.execute("DELETE FROM documents")
            _create_embeddings_table(cursor, new_dim)
    finally:
        conn.close()

def get_file_hash_id(file_path: str) -> str:
    """Returns SHA-256 hash of file_path to act as primary key."""
    # Normalize paths to use forward slashes for consistency
    normalized = file_path.replace("\\", "/").lower()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

def upsert_document(conn, file_path: str, file_name: str, file_extension: str, file_size: int, last_modified: float, content_hash: str) -> str:
    """Inserts or updates document metadata WITHOUT touching its chunks.

    Unlike a delete-and-reinsert, this preserves existing chunk rows so the
    incremental indexer can reuse unchanged chunks' embeddings.
    """
    doc_id = get_file_hash_id(file_path)
    conn.execute("""
        INSERT INTO documents (id, file_path, file_name, file_extension, file_size, last_modified, content_hash, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            file_path = excluded.file_path,
            file_name = excluded.file_name,
            file_extension = excluded.file_extension,
            file_size = excluded.file_size,
            last_modified = excluded.last_modified,
            content_hash = excluded.content_hash,
            indexed_at = CURRENT_TIMESTAMP
    """, (doc_id, file_path.replace("\\", "/"), file_name, file_extension.lower(), file_size,
          datetime.fromtimestamp(last_modified).isoformat(), content_hash))
    return doc_id

def update_document_metadata(file_path: str, file_size: int, last_modified: float):
    """Refreshes only the metadata of an already-indexed document (content unchanged)."""
    doc_id = get_file_hash_id(file_path)
    conn = get_db_connection()
    try:
        conn.execute("""
            UPDATE documents
            SET file_size = ?, last_modified = ?, indexed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (file_size, datetime.fromtimestamp(last_modified).isoformat(), doc_id))
        conn.commit()
    finally:
        conn.close()

def get_document_index_state(file_path: str):
    """Returns (content_hash, {chunk_index: (chunk_id, chunk_hash)}) or None if not indexed."""
    doc_id = get_file_hash_id(file_path)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT content_hash FROM documents WHERE id = ?", (doc_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        content_hash = row[0]
        cursor.execute(
            "SELECT chunk_index, id, chunk_hash FROM document_chunks WHERE document_id = ?",
            (doc_id,)
        )
        chunks = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
        return content_hash, chunks
    finally:
        conn.close()

def insert_chunk(conn, doc_id: str, chunk_index: int, text_content: str, chunk_hash: str) -> int:
    """Inserts a chunk of text and returns the generated chunk ID."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO document_chunks (document_id, chunk_index, text_content, chunk_hash)
        VALUES (?, ?, ?, ?)
    """, (doc_id, chunk_index, text_content, chunk_hash))
    return cursor.lastrowid

def delete_chunks(conn, chunk_ids: list[int]):
    """Deletes specific chunks and their embeddings from the vector table."""
    if not chunk_ids:
        return
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in chunk_ids)
    cursor.execute(f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({placeholders})", chunk_ids)
    cursor.execute(f"DELETE FROM document_chunks WHERE id IN ({placeholders})", chunk_ids)

def insert_embedding(conn, chunk_id: int, embedding: list[float]):
    """Inserts an embedding vector into the virtual vector table."""
    cursor = conn.cursor()
    serialized = sqlite_vec.serialize_float32(embedding)
    cursor.execute("""
        INSERT INTO chunk_embeddings (chunk_id, embedding)
        VALUES (?, ?)
    """, (chunk_id, serialized))

def delete_document_by_path(file_path: str):
    """Deletes a document and all associated chunks and embeddings using cascading delete."""
    doc_id = get_file_hash_id(file_path)
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM document_chunks WHERE document_id = ?", (doc_id,))
    chunk_ids = [row[0] for row in cursor.fetchall()]
    
    if chunk_ids:
        placeholders = ",".join("?" for _ in chunk_ids)
        cursor.execute(f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({placeholders})", chunk_ids)
        
    cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    
    conn.commit()
    conn.close()

def is_document_indexed(file_path: str) -> bool:
    """Returns True if the given file path exists in the documents index.

    Used to restrict the open-file endpoint to files the app actually indexed,
    so an arbitrary local origin cannot launch any path on disk. Matching is
    done via the path hash id, which normalizes slashes and case the same way
    insert_document does.
    """
    doc_id = get_file_hash_id(file_path)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM documents WHERE id = ? LIMIT 1", (doc_id,))
        return cursor.fetchone() is not None
    finally:
        conn.close()

def purge_documents_outside(monitored_dirs: list[str]) -> int:
    """Deletes indexed documents that no longer fall under any monitored dir,
    so removing a folder from the watch list also removes its documents from
    search. Returns the number removed."""
    norm = [d.replace("\\", "/").rstrip("/").lower() for d in monitored_dirs]
    conn = get_db_connection()
    try:
        paths = [r['file_path'] for r in conn.execute("SELECT file_path FROM documents").fetchall()]
    finally:
        conn.close()

    removed = 0
    for fp in paths:
        low = fp.replace("\\", "/").lower()
        if not any(low == d or low.startswith(d + "/") for d in norm):
            delete_document_by_path(fp)
            removed += 1
    return removed

def count_documents() -> int:
    """Returns the number of documents currently indexed."""
    conn = get_db_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()

def get_all_indexed_files() -> dict[str, dict]:
    """Returns a dict mapping file_path to its modification time and size."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT file_path, last_modified, file_size FROM documents")
    results = {}
    for row in cursor.fetchall():
        results[row['file_path']] = {
            'last_modified': row['last_modified'],
            'file_size': row['file_size']
        }
    conn.close()
    return results

def _query_terms(text: str) -> list[str]:
    """Extracts distinct lexical terms (English/number runs and Korean syllable
    runs, length >= 2) from a query, capped to keep lexical scanning cheap."""
    found = re.findall(r"[a-z0-9]{2,}|[가-힣]{2,}", (text or "").lower())
    out = []
    for t in found:
        if t not in out:
            out.append(t)
    return out[:8]

def query_similar_documents(query_text: str, query_vector: list[float], limit: int = 5, file_extensions: list[str] = None, date_from: str = None, date_to: str = None) -> list[dict]:
    """Hybrid search: semantic (vector) similarity re-ranked with a lexical
    keyword boost. Pure dense search is weak for short/acronym/exact-term
    queries (e.g. "dma"); the lexical boost promotes documents that literally
    contain the query terms (especially in the file name)."""
    conn = get_db_connection()
    cursor = conn.cursor()

    serialized_query = sqlite_vec.serialize_float32(query_vector)

    total = cursor.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    if total == 0:
        conn.close()
        return []

    has_filters = bool(file_extensions or date_from or date_to)

    # sqlite-vec KNN is brute-force, so k only bounds returned rows. Over-fetch a
    # larger candidate pool than `limit` so lexical re-ranking can promote a
    # keyword match that scored slightly lower semantically. With metadata
    # filters (applied after the KNN) use the full set to avoid empty results.
    k = total if has_filters else min(limit * 10, total)

    sql = """
        SELECT
            d.id AS doc_id,
            d.file_path,
            d.file_name,
            d.file_extension,
            d.file_size,
            d.last_modified,
            c.text_content,
            c.chunk_index,
            (1.0 - ce.distance) AS similarity
        FROM chunk_embeddings ce
        JOIN document_chunks c ON ce.chunk_id = c.id
        JOIN documents d ON c.document_id = d.id
        WHERE ce.embedding MATCH ? AND k = ?
    """
    params = [serialized_query, k]

    if file_extensions:
        exts_placeholders = ",".join("?" for _ in file_extensions)
        sql += f" AND d.file_extension IN ({exts_placeholders})"
        params.extend([ext.lower() for ext in file_extensions])
    if date_from:
        sql += " AND d.last_modified >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND d.last_modified <= ?"
        params.append(date_to)

    sql += " ORDER BY ce.distance ASC"
    cursor.execute(sql, params)

    # Keep the best-scoring (closest) chunk per document for display.
    cand = {}
    for row in cursor.fetchall():
        path = row['file_path']
        if path not in cand:
            cand[path] = {
                'doc_id': row['doc_id'],
                'file_path': path,
                'file_name': row['file_name'],
                'file_extension': row['file_extension'],
                'file_size': row['file_size'],
                'last_modified': row['last_modified'],
                'text_content': row['text_content'],
                'chunk_index': row['chunk_index'],
                'semantic': row['similarity'],
                'lexical': 0.0,
            }

    # Lexical scoring over the candidate documents. A term found in the file name
    # counts full; in the content, partial. lexical = weighted fraction of terms.
    terms = _query_terms(query_text)
    if terms and cand:
        doc_ids = [c['doc_id'] for c in cand.values()]
        ph = ",".join("?" for _ in doc_ids)
        content_hits = {}
        for term in terms:
            rows = cursor.execute(
                f"SELECT DISTINCT document_id FROM document_chunks "
                f"WHERE document_id IN ({ph}) AND lower(text_content) LIKE ?",
                doc_ids + [f"%{term}%"],
            ).fetchall()
            for r in rows:
                content_hits.setdefault(r[0], set()).add(term)
        for c in cand.values():
            fn = c['file_name'].lower()
            chits = content_hits.get(c['doc_id'], set())
            score = 0.0
            for t in terms:
                if t in fn:
                    score += 1.0
                elif t in chits:
                    score += 0.7
            c['lexical'] = score / len(terms)

    conn.close()

    for c in cand.values():
        c['final'] = c['semantic'] + HYBRID_KEYWORD_WEIGHT * c['lexical']

    ranked = sorted(cand.values(), key=lambda c: c['final'], reverse=True)[:limit]
    return [{
        'file_path': c['file_path'],
        'file_name': c['file_name'],
        'file_extension': c['file_extension'],
        'file_size': c['file_size'],
        'last_modified': c['last_modified'],
        'text_content': c['text_content'],
        'chunk_index': c['chunk_index'],
        'similarity': min(1.0, c['final']),
    } for c in ranked]

import re
_PYINSTALLER_TEMP_RE = re.compile(r"^_MEI\d+$", re.IGNORECASE)

def _filter_watch_dirs(dirs: list[str]) -> list[str]:
    """Drops monitored dirs pointing into a PyInstaller onefile extraction dir
    (a path component like _MEI123456), e.g. a stale test_watch saved by an
    older frozen build."""
    out = []
    for d in dirs:
        try:
            parts = Path(d).parts
        except Exception:
            out.append(d)
            continue
        if any(_PYINSTALLER_TEMP_RE.match(part) for part in parts):
            continue
        out.append(d)
    return out

def get_monitored_dirs() -> list[str]:
    """Retrieves monitored directories from settings, or returns defaults if empty."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'monitored_dirs'")
    row = cursor.fetchone()
    conn.close()

    if row:
        try:
            dirs = json.loads(row['value'])
            cleaned = _filter_watch_dirs(dirs)
            if cleaned != dirs:
                save_monitored_dirs(cleaned)
            return cleaned
        except Exception:
            pass

    from src.config import get_default_watch_dirs
    defaults = get_default_watch_dirs()
    save_monitored_dirs(defaults)
    return defaults

def save_monitored_dirs(dirs: list[str]):
    """Saves monitored directories to settings."""
    import json
    conn = get_db_connection()
    cursor = conn.cursor()
    # Normalize paths
    normalized_dirs = [d.replace("\\", "/") for d in dirs]
    cursor.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('monitored_dirs', ?)",
        (json.dumps(normalized_dirs),)
    )
    conn.commit()
    conn.close()
