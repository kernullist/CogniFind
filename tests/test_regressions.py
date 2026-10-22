"""Regression tests for the 2026-10-22 bug-fix pass.

Run from the project root:
    python -m unittest discover -s tests -v

Every test uses a throwaway SQLite database in a temp dir; the user's real
~/.cognifind/contextfinder.db is never touched.
"""
import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import src.config as config
import src.database as database


class TempDbTestCase(unittest.TestCase):
    """Points src.database at a fresh temp database for each test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cognifind_test_"))
        self._orig_db = database.DB_PATH
        database.DB_PATH = self.tmp / "test.db"
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_document(self, file_path: str, n_chunks: int, dim: int = 384) -> str:
        vec = [1.0] + [0.0] * (dim - 1)
        conn = database.get_db_connection()
        try:
            with conn:
                doc_id = database.upsert_document(
                    conn, file_path, Path(file_path).name, Path(file_path).suffix,
                    123, 1700000000.0, "hash",
                )
                for i in range(n_chunks):
                    cid = database.insert_chunk(conn, doc_id, i, f"chunk text {i}", f"h{i}")
                    database.insert_embedding(conn, cid, vec)
        finally:
            conn.close()
        return doc_id

    def count(self, table: str) -> int:
        conn = database.get_db_connection()
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()


class IgnoredPathTests(unittest.TestCase):
    def test_root_under_ignored_name_is_not_ignored(self):
        # B2: a monitored root below "out"/"vendor" must still be indexed.
        roots = ["D:/out/reports"]
        self.assertFalse(config.is_ignored_path("D:/out/reports/q3.pdf", roots))
        self.assertFalse(config.is_ignored_path("D:\\out\\reports\\sub\\a.md", roots))

    def test_ignored_dir_below_root_is_ignored(self):
        roots = ["D:/out/reports"]
        self.assertTrue(config.is_ignored_path("D:/out/reports/node_modules/x/readme.md", roots))
        self.assertTrue(config.is_ignored_path("D:/out/reports/.git/notes.txt", roots))

    def test_file_named_like_ignored_dir_is_not_ignored(self):
        self.assertFalse(config.is_ignored_path("C:/docs/build", ["C:/docs"]))

    def test_root_itself_is_not_ignored(self):
        self.assertFalse(config.is_ignored_path("D:/out/reports", ["D:/out/reports"]))
        self.assertFalse(config.is_ignored_path("D:/out/reports/", ["D:/out/reports"]))

    def test_without_roots_checks_every_component(self):
        self.assertTrue(config.is_ignored_path("C:/x/build/a.txt"))


class NormalizeWatchDirsTests(unittest.TestCase):
    def test_drops_nested_and_duplicate_dirs(self):
        # B5: nested/duplicate roots would scan and watch files twice.
        dirs = [
            "C:\\Users\\me\\Documents",
            "C:/Users/me",
            "c:/users/me/",
            "D:/Data",
            "D:/Database",
        ]
        self.assertEqual(config.normalize_watch_dirs(dirs), ["C:/Users/me", "D:/Data", "D:/Database"])

    def test_drive_root_keeps_slash_and_covers_children(self):
        self.assertEqual(config.normalize_watch_dirs(["E:/", "E:/sub"]), ["E:/"])


class BatchedDeleteTests(TempDbTestCase):
    def test_delete_document_with_more_chunks_than_sqlite_variable_limit(self):
        # B3: one IN (...) with > 32766 ids raised "too many SQL variables".
        n = 33000
        self.add_document("C:/docs/huge.pdf", n)
        self.assertEqual(self.count("document_chunks"), n)
        database.delete_document_by_path("C:/docs/huge.pdf")
        self.assertEqual(self.count("documents"), 0)
        self.assertEqual(self.count("document_chunks"), 0)
        self.assertEqual(self.count("chunk_embeddings"), 0)

    def test_delete_chunks_many_ids(self):
        self.add_document("C:/docs/a.md", 1200)
        conn = database.get_db_connection()
        try:
            ids = [r[0] for r in conn.execute("SELECT id FROM document_chunks")]
            with conn:
                database.delete_chunks(conn, ids[:1100])
        finally:
            conn.close()
        self.assertEqual(self.count("document_chunks"), 100)
        self.assertEqual(self.count("chunk_embeddings"), 100)


class FolderDeleteTests(TempDbTestCase):
    def test_delete_documents_under_matches_component_boundary(self):
        self.add_document("C:/docs/Reports/a.pdf", 2)
        self.add_document("C:/docs/reports/sub/b.md", 2)
        self.add_document("C:/docs/reports-old/c.md", 2)
        removed = database.delete_documents_under("c:\\docs\\REPORTS")
        self.assertEqual(removed, 2)
        self.assertTrue(database.is_document_indexed("C:/docs/reports-old/c.md"))
        self.assertEqual(self.count("chunk_embeddings"), 2)

    def test_like_wildcards_in_folder_name_are_literal(self):
        self.add_document("C:/docs/a_b/x.md", 1)
        self.add_document("C:/docs/axb/y.md", 1)
        self.add_document("C:/docs/100%/z.md", 1)
        self.add_document("C:/docs/1000/w.md", 1)
        self.assertEqual(database.delete_documents_under("C:/docs/a_b"), 1)
        self.assertEqual(database.delete_documents_under("C:/docs/100%"), 1)
        self.assertTrue(database.is_document_indexed("C:/docs/axb/y.md"))
        self.assertTrue(database.is_document_indexed("C:/docs/1000/w.md"))

    def test_watcher_skips_deletions_inside_ignored_dirs(self):
        from src.watcher import FileWatchHandler
        root = self.tmp / "root"
        worker = SimpleNamespace(monitored_dirs=[root])
        calls = []
        orig = database.delete_documents_under
        import src.watcher as watcher_mod
        watcher_mod.delete_documents_under = lambda p: calls.append(p) or 0
        try:
            handler = FileWatchHandler(worker)
            handler.on_deleted(SimpleNamespace(src_path=str(root / "node_modules" / "pkg" / "index.js"), is_directory=False))
            self.assertEqual(calls, [])
            handler.on_deleted(SimpleNamespace(src_path=str(root / "notes"), is_directory=False))
            self.assertEqual(len(calls), 1)
        finally:
            watcher_mod.delete_documents_under = orig

    def test_watcher_handles_folder_removal_event(self):
        # B1: Windows reports a deleted folder as FileDeletedEvent(<folder>).
        from src.watcher import FileWatchHandler
        self.add_document(str(self.tmp / "gone" / "a.pdf"), 1)
        self.add_document(str(self.tmp / "kept" / "b.pdf"), 1)
        handler = FileWatchHandler(worker=None)
        handler.on_deleted(SimpleNamespace(src_path=str(self.tmp / "gone"), is_directory=False))
        self.assertFalse(database.is_document_indexed(str(self.tmp / "gone" / "a.pdf")))
        self.assertTrue(database.is_document_indexed(str(self.tmp / "kept" / "b.pdf")))

    def test_watcher_keeps_documents_if_folder_still_exists(self):
        from src.watcher import FileWatchHandler
        live = self.tmp / "live"
        live.mkdir()
        self.add_document(str(live / "a.pdf"), 1)
        FileWatchHandler(worker=None).on_deleted(SimpleNamespace(src_path=str(live), is_directory=False))
        self.assertTrue(database.is_document_indexed(str(live / "a.pdf")))


class MonitoredDirsTests(TempDbTestCase):
    def test_save_returns_normalized_and_get_cleans_legacy_value(self):
        self.assertEqual(database.save_monitored_dirs(["C:/a", "C:/a/b"]), ["C:/a"])
        database.set_setting("monitored_dirs", '["C:/x/", "C:/x/y"]')
        self.assertEqual(database.get_monitored_dirs(), ["C:/x"])


class RescanTests(TempDbTestCase):
    def test_scan_and_sync_only_sets_flag(self):
        # B4: the request must not touch walker/db_files from the caller thread.
        from src.watcher import IndexingWorker
        w = IndexingWorker(embedding_engine=None, monitored_dirs=[str(self.tmp)])
        sentinel = object()
        w.walker = sentinel
        w.scanning_complete = True
        w.scan_and_sync()
        self.assertIs(w.walker, sentinel)
        self.assertTrue(w._rescan_requested)
        w._reset_scan()
        self.assertIsNot(w.walker, sentinel)
        self.assertFalse(w.scanning_complete)


class ApiGuardTests(TempDbTestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import api
        cls.api = api
        cls.TestClient = TestClient

    def client(self):
        # No `with`: the lifespan (Qt app, model load) is not started.
        return self.TestClient(self.api.app, base_url="http://127.0.0.1:8765")

    def test_valid_request_passes(self):
        r = self.client().get("/api/model", headers={"Origin": "http://tauri.localhost"})
        self.assertEqual(r.status_code, 200)

    def test_rebound_host_rejected(self):
        # S1: DNS rebinding keeps the attacker's hostname in Host.
        r = self.client().get("/api/settings", headers={"Host": "evil.example:8765"})
        self.assertEqual(r.status_code, 421)

    def test_foreign_origin_rejected(self):
        r = self.client().post("/api/index/scan", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_preflight_from_webview_origin_allowed(self):
        r = self.client().options("/api/search", headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "http://tauri.localhost")

    def test_vite_origin_only_trusted_outside_shipped_app(self):
        # Port 5173 is Vite's default: any unrelated dev server would match.
        dev = "http://localhost:5173"
        self.assertIn(dev, self.api.compute_allowed_origins(False, {}))
        self.assertNotIn(dev, self.api.compute_allowed_origins(True, {}))
        self.assertIn(dev, self.api.compute_allowed_origins(True, {"COGNIFIND_DEV_ORIGIN": "1"}))
        self.assertIn("http://tauri.localhost", self.api.compute_allowed_origins(True, {}))

    def test_no_origin_allowed(self):
        r = self.client().post("/api/index/scan")
        self.assertEqual(r.status_code, 200)


class PdfExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cognifind_pdf_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pymupdf_extracts_text_layer(self):
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF not installed")
        from src.parser import extract_text_from_pdf
        path = self.tmp / "t.pdf"
        doc = fitz.open()
        for word in ("enclave", "hypervisor"):
            page = doc.new_page()
            page.insert_text((72, 72), f"page about {word}")
        doc.save(str(path))
        doc.close()
        text = extract_text_from_pdf(path)
        self.assertIn("enclave", text)
        self.assertIn("hypervisor", text)


if __name__ == "__main__":
    unittest.main()
