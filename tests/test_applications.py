import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import aios.applications as applications
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

    def test_publish_cleanup_rejects_follow_up_writes(self):
        store = self._store()
        created = store.create({"title": "Published", "request": "Make a published page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>published</p>"})
        store.publish({"id": created["id"], "summary": "Published", "keywords": ["published"]})

        with self.assertRaises(ValueError):
            store.write({"id": created["id"], "html": "<!doctype html><p>again</p>"})
        with self.assertRaises(ValueError):
            store.publish({"id": created["id"], "summary": "Again", "keywords": ["published"]})

    def test_interrupted_publish_is_self_healed_before_launch_and_search(self):
        launched = []
        store = self._store(launcher=lambda folder: launched.append(Path(folder)))
        created = store.create({"title": "Recover", "request": "Make a recoverable page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>recover</p>"})
        publish_result = store.publish({"id": created["id"], "summary": "Recoverable", "keywords": ["recover"]})

        folder = self.root / created["id"]
        draft_path = folder / ".draft.json"
        draft_path.write_text(
            json.dumps(
                {
                    "id": created["id"],
                    "title": "Recover",
                    "request": "Make a recoverable page",
                    "created_at": _read_json(folder / "manifest.json")["created_at"],
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        results = store.search({"query": "recover"})
        self.assertEqual(results[0]["id"], created["id"])
        self.assertFalse(draft_path.exists())

        launch_result = store.launch({"id": created["id"]})
        self.assertEqual(launch_result, {"launched": True, "id": created["id"], "title": "Recover"})
        self.assertEqual(launched, [folder])
        self.assertEqual(publish_result["sha256"], _read_json(folder / "manifest.json")["sha256"])

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

    def test_existing_non_directory_root_rejected(self):
        root = Path(self.tmp.name) / "applications-root"
        root.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ValueError):
            ApplicationStore(root=root)

    def test_created_directories_are_hardened(self):
        original_umask = os.umask(0)
        try:
            nested_root = Path(self.tmp.name) / "secure" / "applications"
            store = ApplicationStore(root=nested_root)
        finally:
            os.umask(original_umask)

        self.assertEqual(stat.S_IMODE((Path(self.tmp.name) / "secure").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(nested_root.stat().st_mode), 0o700)

        created = store.create({"title": "Hardened", "request": "Make a hardened page"})
        self.assertEqual(stat.S_IMODE((nested_root / created["id"]).stat().st_mode), 0o700)

    def test_oversize_html_rejected(self):
        store = self._store()
        created = store.create({"title": "Big", "request": "Make a big page"})
        html = "<!doctype html>" + ("x" * (16 * 1024))
        with self.assertRaises(ValueError):
            store.write({"id": created["id"], "html": html})

    def test_exactly_sixteen_kib_html_is_accepted(self):
        store = self._store()
        created = store.create({"title": "Bound", "request": "Make a bounded page"})
        html = "<!doctype html>" + ("x" * (16 * 1024 - len("<!doctype html>")))
        result = store.write({"id": created["id"], "html": html})
        self.assertEqual(result["written"], True)
        self.assertEqual(result["bytes"], 16 * 1024)
        self.assertEqual(store.read({"id": created["id"]}), html)

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
        with self.assertRaises(ValueError):
            store.publish({"id": created["id"], "summary": "Good", "keywords": ["alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi"]})

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

    def test_search_results_are_capped_at_five(self):
        store = self._store()
        for index in range(6):
            created = store.create({"title": f"Shared App {index}", "request": f"Make shared app {index}"})
            store.write({"id": created["id"], "html": f"<!doctype html><p>{index}</p>"})
            store.publish({"id": created["id"], "summary": f"Shared summary {index}", "keywords": ["shared"]})

        results = store.search({"query": "shared"})
        self.assertEqual(len(results), 5)

    def test_regular_file_reader_rejects_symlinked_index(self):
        helper = getattr(applications, "_read_regular_file_bytes", None)
        self.assertIsNotNone(helper)
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        store = self._store()
        created = store.create({"title": "Helper", "request": "Make a helper page"})
        folder = self.root / created["id"]
        target = folder / "payload.txt"
        target.write_text("payload", encoding="utf-8")
        index = folder / "index.html"
        os.symlink(target, index)

        with self.assertRaises(ValueError):
            helper(index, 16 * 1024)

    def test_regular_file_reader_repeated_errors_do_not_leak_file_descriptors(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("requires Linux")
        fd_dir = Path("/proc/self/fd")
        if not fd_dir.is_dir():
            self.skipTest("/proc/self/fd unavailable")
        helper = getattr(applications, "_read_regular_file_bytes", None)
        self.assertIsNotNone(helper)
        path = Path(self.tmp.name) / "oversized.bin"
        path.write_bytes(b"x" * 32)

        baseline = len(os.listdir(fd_dir))
        for _ in range(25):
            with self.assertRaisesRegex(ValueError, "too large"):
                helper(path, 1)
            self.assertEqual(len(os.listdir(fd_dir)), baseline)

    def test_search_ignores_symlinked_application_folder(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        store = self._store()
        external_root = Path(self.tmp.name) / "outside"
        external_store = ApplicationStore(root=external_root)
        created = external_store.create({"title": "Outside", "request": "Build the outside app"})
        external_store.write({"id": created["id"], "html": "<!doctype html><p>outside</p>"})
        external_store.publish({"id": created["id"], "summary": "Outside summary", "keywords": ["outside"]})
        try:
            os.symlink(external_root / created["id"], self.root / created["id"], target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("environment cannot create symlinks")

        self.assertEqual(store.search({"query": "Build the outside app"}), [])

    def test_search_rejects_folder_swapped_to_symlink_after_validation(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        store = self._store()
        created = store.create({"title": "Swap", "request": "Build the swap app"})
        store.write({"id": created["id"], "html": "<!doctype html><p>swap</p>"})
        store.publish({"id": created["id"], "summary": "Swap summary", "keywords": ["swap"]})

        external_root = Path(self.tmp.name) / "outside"
        external_folder = external_root / created["id"]
        shutil.copytree(self.root / created["id"], external_folder)
        draft_path = external_folder / ".draft.json"
        draft_path.write_text(
            json.dumps({
                "id": created["id"],
                "title": "Swap",
                "request": "Build the swap app",
                "created_at": _read_json(external_folder / "manifest.json")["created_at"],
            }, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

        original_load_manifest = store._load_manifest
        swapped = False

        def swapping_load_manifest(folder: Path):
            nonlocal swapped
            if not swapped and folder.name == created["id"]:
                swapped = True
                shutil.rmtree(folder)
                os.symlink(external_folder, folder, target_is_directory=True)
            return original_load_manifest(folder)

        store._load_manifest = swapping_load_manifest  # type: ignore[method-assign]
        self.assertEqual(store.search({"query": "Build the swap app"}), [])
        self.assertTrue(draft_path.exists())

    def test_linux_rejects_symlinked_intermediate_root_component(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("requires Linux")
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        outside = Path(self.tmp.name) / "outside"
        applications_dir = outside / "applications"
        applications_dir.mkdir(parents=True)
        os.chmod(applications_dir, 0o755)
        link = Path(self.tmp.name) / "link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("environment cannot create symlinks")

        with self.assertRaises(ValueError):
            ApplicationStore(root=link / "applications" / "nested")
        self.assertFalse((applications_dir / "nested").exists())
        self.assertEqual(stat.S_IMODE(applications_dir.stat().st_mode), 0o755)

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
