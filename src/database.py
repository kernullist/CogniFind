import sqlite3
import sqlite_vec
import hashlib
from datetime import datetime
from src.config import DB_PATH

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

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA foreign_keys = ON;")
    
    # 1. documents table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        file_path TEXT NOT NULL UNIQUE,
        file_name TEXT NOT NULL,
        file_extension TEXT NOT NULL,
        file_size INTEGER NOT NULL,
        last_modified TIMESTAMP NOT NULL,
        indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    # 2. document_chunks table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS document_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        text_content TEXT NOT NULL,
        FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
    );
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON document_chunks(document_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_documents_file_path ON documents(file_path);")
    
    # 3. chunk_embeddings virtual table (sqlite-vec)
    cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_embeddings USING vec0(
        chunk_id INTEGER PRIMARY KEY,
        embedding float[384] distance_metric=cosine
    );
    """)
    
    # 4. settings table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    
    conn.commit()
    conn.close()

def get_file_hash_id(file_path: str) -> str:
    """Returns SHA-256 hash of file_path to act as primary key."""
    # Normalize paths to use forward slashes for consistency
    normalized = file_path.replace("\\", "/").lower()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

def insert_document(conn, file_path: str, file_name: str, file_extension: str, file_size: int, last_modified: float) -> str:
    """Inserts metadata for a document and returns the document ID."""
    doc_id = get_file_hash_id(file_path)
    cursor = conn.cursor()
    # Delete first to trigger CASCADE deletes on chunks (which we also manually delete from virtual table)
    # Get all chunk IDs first to delete from virtual table
    cursor.execute("SELECT id FROM document_chunks WHERE document_id = ?", (doc_id,))
    chunk_ids = [row[0] for row in cursor.fetchall()]
    
    if chunk_ids:
        placeholders = ",".join("?" for _ in chunk_ids)
        cursor.execute(f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({placeholders})", chunk_ids)
        
    cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    
    cursor.execute("""
        INSERT INTO documents (id, file_path, file_name, file_extension, file_size, last_modified)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (doc_id, file_path.replace("\\", "/"), file_name, file_extension.lower(), file_size, datetime.fromtimestamp(last_modified).isoformat()))
    return doc_id

def insert_chunk(conn, doc_id: str, chunk_index: int, text_content: str) -> int:
    """Inserts a chunk of text and returns the generated chunk ID."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO document_chunks (document_id, chunk_index, text_content)
        VALUES (?, ?, ?)
    """, (doc_id, chunk_index, text_content))
    return cursor.lastrowid

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

def query_similar_documents(query_vector: list[float], limit: int = 5, file_extensions: list[str] = None, date_from: str = None, date_to: str = None) -> list[dict]:
    """Performs KNN vector similarity search joined with document metadata."""
    conn = get_db_connection()
    cursor = conn.cursor()

    serialized_query = sqlite_vec.serialize_float32(query_vector)

    # Total number of candidate vectors. Used to size the KNN fetch.
    total = cursor.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    if total == 0:
        conn.close()
        return []

    has_filters = bool(file_extensions or date_from or date_to)

    # The metadata filters (extension/date) are applied AFTER the vec0 KNN
    # returns its top-k rows, so a small k can be entirely filtered out and
    # yield far fewer than `limit` results even when matching documents exist.
    # sqlite-vec KNN is brute-force (it scores every vector regardless of k),
    # so when filters are present we set k to the full candidate count to keep
    # results correct; the only extra cost is sorting/returning more rows.
    # Without filters we keep the cheap limit*3 over-fetch for de-duplication.
    if has_filters:
        k = total
    else:
        k = min(limit * 3, total)

    sql = """
        SELECT
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

    results = []
    seen_files = {}

    for row in cursor.fetchall():
        path = row['file_path']
        sim = row['similarity']

        if path not in seen_files:
            seen_files[path] = sim
            results.append({
                'file_path': path,
                'file_name': row['file_name'],
                'file_extension': row['file_extension'],
                'file_size': row['file_size'],
                'last_modified': row['last_modified'],
                'text_content': row['text_content'],
                'chunk_index': row['chunk_index'],
                'similarity': sim
            })
            if len(results) >= limit:
                break

    conn.close()
    return results

def get_monitored_dirs() -> list[str]:
    """Retrieves monitored directories from settings, or returns defaults if empty."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'monitored_dirs'")
    row = cursor.fetchone()
    conn.close()
    
    if row:
        import json
        try:
            return json.loads(row['value'])
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
