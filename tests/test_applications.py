import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from aios.applications import APPLICATION_TOOL, ApplicationStore


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class ApplicationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "applications"

    def _store(self, launcher=None):
        return ApplicationStore(root=self.root, launcher=launcher)

    def test_tool_schema_rejects_additional_properties(self):
        parameters = APPLICATION_TOOL["function"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertIn("action", parameters["properties"])
        self.assertIn("query", parameters["properties"])
        self.assertIn("id", parameters["properties"])
        self.assertIn("title", parameters["properties"])
        self.assertIn("request", parameters["properties"])
        self.assertIn("html", parameters["properties"])
        self.assertIn("summary", parameters["properties"])
        self.assertIn("keywords", parameters["properties"])

    def test_create_write_publish_search_launch_happy_path(self):
        launched = []
        store = self._store(launcher=lambda folder: launched.append(Path(folder)))
        created = store.create({"title": "Hello World", "request": "Build a hello page"})
        self.assertRegex(created["id"], r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{8}$")
        self.assertEqual(created["title"], "Hello World")

        folder = self.root / created["id"]
        self.assertTrue(folder.is_dir())
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        draft = _read_json(folder / ".draft.json")
        self.assertEqual(draft["id"], created["id"])
        self.assertEqual(draft["title"], "Hello World")
        self.assertEqual(draft["request"], "Build a hello page")
        self.assertEqual(stat.S_IMODE((folder / ".draft.json").stat().st_mode), 0o600)

        write_result = store.write({"id": created["id"], "html": "<!doctype html><title>Hello</title>"})
        self.assertEqual(write_result["written"], True)
        self.assertGreater(write_result["bytes"], 0)
        self.assertEqual(store.read({"id": created["id"]}), "<!doctype html><title>Hello</title>")
        self.assertEqual(stat.S_IMODE((folder / "index.html").stat().st_mode), 0o600)

        publish_result = store.publish({
            "id": created["id"],
            "summary": "A hello page",
            "keywords": ["hello", "world", "hello"],
        })
        self.assertTrue(publish_result["published"])
        self.assertEqual(publish_result["id"], created["id"])
        self.assertRegex(publish_result["sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse((folder / ".draft.json").exists())
        manifest = _read_json(folder / "manifest.json")
        self.assertEqual(manifest["keywords"], ["hello", "world"])
        self.assertEqual(manifest["entrypoint"], "index.html")
        self.assertEqual(manifest["sha256"], publish_result["sha256"])
        self.assertEqual(stat.S_IMODE((folder / "manifest.json").stat().st_mode), 0o600)

        matches = store.search({"query": "Build a hello page"})
        self.assertEqual(matches[0]["id"], created["id"])
        self.assertTrue(matches[0]["exact"])
        self.assertEqual(matches[0]["title"], "Hello World")

        launch_result = store.launch({"id": created["id"]})
        self.assertEqual(launch_result, {"launched": True, "id": created["id"], "title": "Hello World"})
        self.assertEqual(launched, [folder])

    def test_exact_request_ranks_above_keyword_overlap(self):
        store = self._store()
        exact = store.create({"title": "Blue Sky", "request": "Make a blue sky gallery"})
        keyword = store.create({"title": "Gallery", "request": "Make a gallery"})
        store.write({"id": exact["id"], "html": "<!doctype html><p>exact</p>"})
        store.write({"id": keyword["id"], "html": "<!doctype html><p>keyword</p>"})
        store.publish({"id": exact["id"], "summary": "Exact", "keywords": ["gallery", "blue"]})
        store.publish({"id": keyword["id"], "summary": "Keyword", "keywords": ["blue", "sky", "gallery"]})

        results = store.search({"query": "Make a blue sky gallery"})
        self.assertEqual(results[0]["id"], exact["id"])
        self.assertTrue(results[0]["exact"])
        self.assertFalse(results[1]["exact"])

    def test_invalid_and_traversal_id_rejected(self):
        store = self._store()
        for value in ["", "bad", "abc-123", "../escape", "/abs/path", "hello-1234567g"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    store.read({"id": value})

    def test_symlink_rejection(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        target = Path(self.tmp.name) / "target"
        target.mkdir()
        link = self.root
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("environment cannot create symlinks")
        with self.assertRaises(ValueError):
            ApplicationStore(root=link)

    def test_oversize_html_rejected(self):
        store = self._store()
        created = store.create({"title": "Big", "request": "Make a big page"})
        html = "<!doctype html>" + ("x" * (16 * 1024))
        with self.assertRaises(ValueError):
            store.write({"id": created["id"], "html": html})

    def test_malformed_and_extra_file_rejection(self):
        store = self._store()
        created = store.create({"title": "Bad", "request": "Make a bad page"})
        folder = self.root / created["id"]
        (folder / "extra.txt").write_text("nope", encoding="utf-8")
        with self.assertRaises(ValueError):
            store.write({"id": created["id"], "html": "<!doctype html><p>bad</p>"})
        (folder / "extra.txt").unlink()
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            store.publish({"id": created["id"], "summary": "Summary", "keywords": ["x"]})

    def test_invalid_keyword_and_summary_metadata(self):
        store = self._store()
        created = store.create({"title": "Meta", "request": "Make a meta page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>meta</p>"})
        for summary in ["", " " * 3, "x" * 301]:
            with self.subTest(summary=summary):
                with self.assertRaises(ValueError):
                    store.publish({"id": created["id"], "summary": summary, "keywords": ["ok"]})
        with self.assertRaises(ValueError):
            store.publish({"id": created["id"], "summary": "Good", "keywords": ["", "ok"]})
        with self.assertRaises(ValueError):
            store.publish({"id": created["id"], "summary": "Good", "keywords": ["x"] * 21})

    def test_digest_mismatch_blocks_launch(self):
        launched = []
        store = self._store(launcher=lambda folder: launched.append(Path(folder)))
        created = store.create({"title": "Launch", "request": "Make a launch page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>launch</p>"})
        store.publish({"id": created["id"], "summary": "Launchable", "keywords": ["launch"]})
        (self.root / created["id"] / "index.html").write_text("<!doctype html><p>changed</p>", encoding="utf-8")
        with self.assertRaises(ValueError):
            store.launch({"id": created["id"]})
        self.assertEqual(launched, [])
        self.assertTrue((self.root / created["id"] / "manifest.json").exists())

    def test_launcher_failure_preserves_published_files(self):
        def launcher(_folder):
            raise RuntimeError("boom")

        store = self._store(launcher=launcher)
        created = store.create({"title": "Keep", "request": "Keep a page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>keep</p>"})
        store.publish({"id": created["id"], "summary": "Kept", "keywords": ["keep"]})
        manifest = (self.root / created["id"] / "manifest.json").read_text(encoding="utf-8")
        with self.assertRaises(RuntimeError):
            store.launch({"id": created["id"]})
        self.assertEqual((self.root / created["id"] / "manifest.json").read_text(encoding="utf-8"), manifest)
        self.assertTrue((self.root / created["id"] / "index.html").exists())

    def test_atomic_temp_cleanup(self):
        store = self._store()
        created = store.create({"title": "Cleanup", "request": "Make a cleanup page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>cleanup</p>"})
        store.publish({"id": created["id"], "summary": "Clean", "keywords": ["cleanup"]})
        self.assertEqual({path.name for path in (self.root / created["id"]).iterdir()}, {"index.html", "manifest.json"})


if __name__ == "__main__":
    unittest.main()
