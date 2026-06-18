"""Inspect the CogniFind index for debugging.

Usage:
    python scripts/inspect_index.py              # summary + recently indexed docs + integrity checks
    python scripts/inspect_index.py --all        # list every indexed document
    python scripts/inspect_index.py <substring>  # dump the extracted chunk text of matching documents

Reads the same database the app writes (~/.cognifind/contextfinder.db), so it
works while the app is running (SQLite WAL allows concurrent readers).
"""
import sys
from pathlib import Path

# Emit UTF-8 so non-ASCII (e.g. Korean) file paths print correctly. On a console
# still set to a legacy codepage, run `chcp 65001` first or use Windows Terminal.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DB_PATH
from src.database import get_db_connection, get_active_model_key, get_monitored_dirs


def fmt_size(n: int) -> str:
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.1f} MB"


def short_ts(s) -> str:
    return (str(s)[:19]) if s else "-"


def dump_chunks(conn, needle: str):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, file_path FROM documents WHERE file_path LIKE ? ORDER BY file_path",
        (f"%{needle}%",),
    )
    docs = cur.fetchall()
    if not docs:
        print(f"No indexed document matches '{needle}'.")
        return
    for doc in docs:
        cur.execute(
            "SELECT chunk_index, text_content FROM document_chunks WHERE document_id = ? ORDER BY chunk_index",
            (doc["id"],),
        )
        chunks = cur.fetchall()
        print(f"\n=== {doc['file_path']}  ({len(chunks)} chunks) ===")
        for ch in chunks:
            text = " ".join(ch["text_content"].split())
            preview = text[:160] + ("..." if len(text) > 160 else "")
            print(f"  [{ch['chunk_index']:>3}] {preview}")


def main():
    args = sys.argv[1:]
    show_all = "--all" in args
    needle = next((a for a in args if not a.startswith("-")), None)

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        if needle:
            dump_chunks(conn, needle)
            return

        n_docs = cur.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        n_chunks = cur.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        n_emb = cur.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]

        print(f"CogniFind index: {DB_PATH}")
        print(f"Active model:    {get_active_model_key()}")
        print(f"Monitored dirs:  {get_monitored_dirs()}")
        print()
        print(f"Documents:  {n_docs}")
        print(f"Chunks:     {n_chunks}")
        emb_note = "OK: matches chunks" if n_emb == n_chunks else "MISMATCH: should equal chunks"
        print(f"Embeddings: {n_emb}   ({emb_note})")
        print()

        rows = cur.execute(
            """
            SELECT d.file_path, d.file_extension, d.file_size, d.last_modified,
                   d.indexed_at, COUNT(c.id) AS chunks
            FROM documents d
            LEFT JOIN document_chunks c ON c.document_id = d.id
            GROUP BY d.id
            ORDER BY d.indexed_at DESC
            """
        ).fetchall()

        shown = rows if show_all else rows[:30]
        title = "All indexed documents:" if show_all else f"Recently indexed (showing {len(shown)} of {n_docs}):"
        print(title)
        for r in shown:
            print(
                f"  {short_ts(r['indexed_at'])}  {r['chunks']:>4} chunks  "
                f"{fmt_size(r['file_size']):>9}  {r['file_path']}"
            )

        empty = [r for r in rows if r["chunks"] == 0]
        if empty:
            print()
            print(f"[!] {len(empty)} document(s) with 0 chunks (empty file or extraction failed):")
            for r in empty:
                print(f"    {r['file_path']}")

        if not show_all and n_docs > len(shown):
            print(f"\n(use --all to list all {n_docs}, or pass a path substring to dump chunk text)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
