import json
import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from threading import Event, Thread
from unittest import mock
from urllib import error, request

import aios.applications as applications
from aios.applications import APPLICATION_TOOL, ApplicationStore


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class _IframeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.iframes = []
        self.titles = []

    def handle_starttag(self, tag, attrs):
        if tag == "iframe":
            self.iframes.append(dict(attrs))

    def handle_data(self, data):
        self.titles.append(data)


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

    def test_fallback_rejects_symlinked_intermediate_component(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        outside = Path(self.tmp.name) / "outside"
        real_subdir = outside / "sub"
        real_subdir.mkdir(parents=True)
        os.chmod(real_subdir, 0o755)
        link = Path(self.tmp.name) / "link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("environment cannot create symlinks")

        with mock.patch.object(applications, "_supports_descriptor_safe_directories", return_value=False):
            with self.assertRaises(ValueError):
                ApplicationStore(root=link / "sub" / "newdir")

        self.assertFalse((real_subdir / "newdir").exists())
        self.assertEqual(sorted(path.name for path in real_subdir.iterdir()), [])

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

    def test_handler_serves_wrapper_and_app_with_security_headers(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Wrapper", "request": "Build a wrapper page"})
        html = "<!doctype html><title>App</title><p>hello</p>"
        store.write({"id": created["id"], "html": html})
        store.publish({"id": created["id"], "summary": "Wrapper summary", "keywords": ["wrapper"]})
        document = app_runner.load_document(self.root / created["id"])
        handler = app_runner.handler_for(document)

        with self._serve(handler) as base_url:
            wrapper = self._get(base_url + "/")
            self.assertEqual(wrapper["status"], 200)
            self.assertEqual(wrapper["headers"]["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(wrapper["headers"]["Cache-Control"], "no-store")
            self.assertEqual(wrapper["headers"]["X-Content-Type-Options"], "nosniff")
            self.assertEqual(wrapper["headers"]["Referrer-Policy"], "no-referrer")
            self.assertEqual(
                wrapper["headers"]["Content-Security-Policy"],
                "default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; base-uri 'none'; object-src 'none'; form-action 'none'",
            )
            parser = _IframeParser()
            parser.feed(wrapper["body"].decode("utf-8"))
            self.assertEqual(len(parser.iframes), 1)
            self.assertEqual(parser.iframes[0]["sandbox"], "allow-scripts")
            self.assertEqual(parser.iframes[0]["src"], "/app")
            self.assertIn("title", parser.iframes[0])
            self.assertTrue(parser.iframes[0]["title"].strip())

            app = self._get(base_url + "/app")
            self.assertEqual(app["status"], 200)
            self.assertEqual(app["headers"]["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(app["headers"]["Cache-Control"], "no-store")
            self.assertEqual(app["headers"]["X-Content-Type-Options"], "nosniff")
            self.assertEqual(app["headers"]["Referrer-Policy"], "no-referrer")
            self.assertEqual(
                app["headers"]["Content-Security-Policy"],
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; font-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'",
            )
            self.assertEqual(app["body"].decode("utf-8"), html)

    def test_handler_rejects_unknown_and_query_paths(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Paths", "request": "Build a path page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>paths</p>"})
        store.publish({"id": created["id"], "summary": "Path summary", "keywords": ["paths"]})
        document = app_runner.load_document(self.root / created["id"])
        handler = app_runner.handler_for(document)

        with self._serve(handler) as base_url:
            for path in ["/missing", "/?x=1", "/app?x=1", "/app/extra"]:
                with self.subTest(path=path):
                    result = self._get(base_url + path, expect_error=True)
                    self.assertEqual(result["status"], 404)

    def test_handler_content_length_uses_utf8_bytes(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Unicode", "request": "Build a unicode page"})
        html = "<!doctype html><meta charset=\"utf-8\"><p>café — 漢字</p>"
        store.write({"id": created["id"], "html": html})
        store.publish({"id": created["id"], "summary": "Unicode summary", "keywords": ["unicode"]})
        document = app_runner.load_document(self.root / created["id"])
        handler = app_runner.handler_for(document)

        with self._serve(handler) as base_url:
            result = self._get(base_url + "/app")
            self.assertEqual(result["status"], 200)
            self.assertEqual(result["headers"]["Content-Length"], str(len(result["body"])))
            self.assertEqual(result["body"].decode("utf-8"), html)

    def test_load_document_accepts_published_app_and_rejects_digest_or_symlinks(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Published", "request": "Build a published page"})
        html = "<!doctype html><p>published café</p>"
        store.write({"id": created["id"], "html": html})
        store.publish({"id": created["id"], "summary": "Published summary", "keywords": ["published"]})
        folder = self.root / created["id"]

        self.assertEqual(app_runner.load_document(folder), html)

        folder.joinpath("index.html").write_text("<!doctype html><p>tampered</p>", encoding="utf-8")
        with self.assertRaises(ValueError):
            app_runner.load_document(folder)

        if hasattr(os, "symlink"):
            link_root = Path(self.tmp.name) / "links"
            link_root.mkdir()
            try:
                os.symlink(folder, link_root / "folder", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("environment cannot create symlinks")
            with self.assertRaises(ValueError):
                app_runner.load_document(link_root / "folder")

            linked_file_root = Path(self.tmp.name) / "linked-file"
            linked_file_root.mkdir()
            shutil.copy(folder / "manifest.json", linked_file_root / "manifest.json")
            try:
                os.symlink(folder / "index.html", linked_file_root / "index.html")
            except (OSError, NotImplementedError):
                self.skipTest("environment cannot create symlinks")
            with self.assertRaises(ValueError):
                app_runner.load_document(linked_file_root)

    def test_run_builds_restricted_chromium_command_and_cleans_up_on_launcher_failure(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Launch", "request": "Build a launch page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>launch</p>"})
        store.publish({"id": created["id"], "summary": "Launch summary", "keywords": ["launch"]})
        folder = self.root / created["id"]

        state, server_class = self._fake_server_class()
        proc = mock.Mock()
        proc.wait.return_value = 0
        proc.pid = 4321
        with mock.patch.object(app_runner, "ThreadingHTTPServer", server_class), \
            mock.patch.object(app_runner.subprocess, "Popen", return_value=proc) as popen:
            app_runner.run(folder)

        args = popen.call_args.args[0]
        self.assertIsNotNone(state["server"])
        self.assertIn(f"--app=http://127.0.0.1:{state['server'].server_address[1]}/", args)
        self.assertTrue(any(arg.startswith("--user-data-dir=") for arg in args))
        self.assertTrue(any(arg.startswith("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1") for arg in args))
        self.assertTrue(any(arg.startswith("--host-resolver-rules=") and "::1" in arg for arg in args))
        self.assertIn("--disable-dev-shm-usage", args)
        self.assertIn("--no-first-run", args)
        self.assertIn("--no-default-browser-check", args)
        self.assertNotIn("--no-sandbox", args)

    def test_run_shuts_down_server_when_chromium_launch_raises(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Boom", "request": "Build a boom page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>boom</p>"})
        store.publish({"id": created["id"], "summary": "Boom summary", "keywords": ["boom"]})
        folder = self.root / created["id"]

        state, server_class = self._fake_server_class()
        with mock.patch.object(app_runner, "ThreadingHTTPServer", server_class), \
            mock.patch.object(app_runner.subprocess, "Popen", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                app_runner.run(folder)

        self.assertIsNotNone(state["server"])
        self.assertTrue(state["server"].shutdown_called)
        self.assertTrue(state["server"].server_close_called)
        self.assertTrue(state["server"].joined.is_set())

    def test_run_shuts_down_server_when_wait_is_interrupted(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Interrupt", "request": "Build an interrupt page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>interrupt</p>"})
        store.publish({"id": created["id"], "summary": "Interrupt summary", "keywords": ["interrupt"]})
        folder = self.root / created["id"]

        state, server_class = self._fake_server_class()
        proc = mock.Mock()
        proc.pid = 8765
        proc.wait.side_effect = KeyboardInterrupt
        with mock.patch.object(app_runner, "ThreadingHTTPServer", server_class), \
            mock.patch.object(app_runner.subprocess, "Popen", return_value=proc):
            with self.assertRaises(KeyboardInterrupt):
                app_runner.run(folder)

        self.assertIsNotNone(state["server"])
        self.assertTrue(state["server"].shutdown_called)
        self.assertTrue(state["server"].server_close_called)
        self.assertTrue(state["server"].joined.is_set())

    def test_main_requires_exactly_one_folder_argument(self):
        from aios import app_runner

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(app_runner.main([]), 0)
            self.assertNotEqual(app_runner.main(["one", "two"]), 0)

    def _get(self, url, expect_error=False):
        req = request.Request(url)
        try:
            with request.urlopen(req, timeout=5) as response:
                return {
                    "status": response.status,
                    "headers": dict(response.headers.items()),
                    "body": response.read(),
                }
        except error.HTTPError as exc:
            if not expect_error:
                raise
            body = exc.read()
            return {"status": exc.code, "headers": dict(exc.headers.items()), "body": body}

    def _serve(self, handler):
        from aios import app_runner

        server = app_runner.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        class _ServeContext:
            def __enter__(self_nonlocal):
                for _ in range(50):
                    try:
                        request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/", timeout=0.1).close()
                        break
                    except Exception:
                        pass
                return f"http://127.0.0.1:{server.server_address[1]}"

            def __exit__(self_nonlocal, exc_type, exc, tb):
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        return _ServeContext()

    def _fake_server_class(self):
        state = {"server": None}

        class FakeServer:
            def __init__(self, address, handler):
                self.server_address = ("127.0.0.1", 49152)
                self.shutdown_called = False
                self.server_close_called = False
                self._stop = Event()
                self.joined = Event()
                state["server"] = self

            def serve_forever(self):
                self._stop.wait(timeout=5)
                self.joined.set()

            def shutdown(self):
                self.shutdown_called = True
                self._stop.set()

            def server_close(self):
                self.server_close_called = True
                self._stop.set()

        return state, FakeServer


if __name__ == "__main__":
    unittest.main()
