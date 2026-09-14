import json
import contextlib
import io
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from threading import Event, Thread
from unittest import mock
from urllib import error, request

import aios.applications as applications
from aios.applications import APPLICATION_TOOL, NATIVE_TEMPLATES, ApplicationStore, application_tool


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

    def _store(self, launcher=None, **kwargs):
        store = ApplicationStore(root=self.root, launcher=launcher, **kwargs)
        self.addCleanup(store.close)
        return store

    def _native_host(self, body):
        path = Path(self.tmp.name) / "aios-app-host"
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o700)
        return path

    def _write_executable(self, name, body):
        path = Path(self.tmp.name) / name
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o700)
        return path

    def _wait_for_path(self, path: Path, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                try:
                    _read_json(path)
                    return
                except (OSError, json.JSONDecodeError):
                    pass
            time.sleep(0.05)
        self.fail(f"Timed out waiting for {path}")

    def _wait_for_pid_exit(self, pid: int, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"Timed out waiting for pid {pid} to exit")

    def test_tool_schema_rejects_additional_properties(self):
        parameters = APPLICATION_TOOL["function"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertIn("action", parameters["properties"])
        self.assertIn("build", parameters["properties"]["action"]["enum"])
        self.assertIn("query", parameters["properties"])
        self.assertIn("id", parameters["properties"])
        self.assertIn("title", parameters["properties"])
        self.assertIn("request", parameters["properties"])
        self.assertIn("html", parameters["properties"])
        self.assertIn("summary", parameters["properties"])
        self.assertIn("keywords", parameters["properties"])
        self.assertNotIn("runtime", parameters["properties"])
        self.assertNotIn("template", parameters["properties"])

    def test_capability_tool_schema_is_deep_independent(self):
        native = application_tool(("calculator",))
        properties = native["function"]["parameters"]["properties"]
        self.assertEqual(properties["runtime"]["enum"], ["web", "native"])
        self.assertEqual(properties["template"]["enum"], ["calculator"])
        properties["action"]["enum"].append("bad")
        properties["template"]["enum"].append("bad")
        second = application_tool(("calculator",))
        self.assertNotIn("bad", APPLICATION_TOOL["function"]["parameters"]["properties"]["action"]["enum"])
        self.assertEqual(second["function"]["parameters"]["properties"]["template"]["enum"], ["calculator"])

    def test_draft_web_launch_uses_validated_snapshot_without_publishing(self):
        from aios.app_runner import load_document

        launched = []
        html = "<!doctype html><title>Draft</title>"

        def launch(folder):
            launched.append(folder)
            self.assertNotEqual(folder.parent, self.root)
            self.assertEqual(load_document(folder), html)
            self.assertEqual({path.name for path in folder.iterdir()}, {"manifest.json", "index.html"})
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            return True

        store = self._store(launcher=launch)
        app = store.create({"title": "Draft", "request": "Create a draft app"})
        store.write({"id": app["id"], "html": html})
        self.assertTrue(store.launch({"id": app["id"]})["launched"])
        self.assertEqual(launched[0].name, app["id"])
        self.assertFalse(launched[0].exists())
        folder = self.root / app["id"]
        self.assertEqual({path.name for path in folder.iterdir()}, {".draft.json", "index.html"})
        self.assertEqual(store.search({"query": "Create a draft app"})[0]["id"], app["id"])
        self.assertEqual(self._store(launcher=launch).search({"query": "Create a draft app"}), [])
        store.write({"id": app["id"], "html": "<!doctype html><title>Edited</title>"})
        self.assertTrue(store.publish({"id": app["id"], "summary": "Edited app", "keywords": ["draft"]})["published"])
        self.assertEqual(store.search({"query": "Create a draft app"})[0]["id"], app["id"])

    def test_build_atomically_creates_writes_and_launches_generic_web_app(self):
        from aios.app_runner import load_document

        launched = []
        html = "<!doctype html><title>Text Editor</title><textarea></textarea>"

        def launch(folder):
            launched.append(Path(folder))
            self.assertEqual(load_document(folder), html)
            return True

        store = self._store(launcher=launch)
        result = store.build({
            "action": "build",
            "title": "Text Editor",
            "request": "create a text editor app",
            "runtime": "web",
            "html": html,
        })
        self.assertTrue(result["written"])
        self.assertTrue(result["launched"])
        self.assertEqual(result["runtime"], "web")
        self.assertEqual(store.search({"query": "create a text editor app"})[0]["id"], result["id"])
        self.assertEqual(len(launched), 1)

    def test_draft_native_launch_remains_unpublished_until_explicit_publish(self):
        launched = []

        def launch(folder):
            launched.append(folder)
            self.assertEqual({path.name for path in folder.iterdir()}, {"manifest.json"})
            self.assertEqual(_read_json(folder / "manifest.json")["template"], "calculator")
            return True

        store = self._store(launcher=launch, native_host=self._native_host(""))
        app = store.create({
            "title": "Calculator", "request": "create a calculator application",
            "runtime": "native", "template": "calculator",
        })
        self.assertTrue(store.launch({"id": app["id"]})["launched"])
        self.assertEqual(len(launched), 1)
        self.assertFalse(launched[0].exists())
        self.assertEqual({path.name for path in (self.root / app["id"]).iterdir()}, {".draft.json"})
        match = store.search({"query": "calculator"})[0]
        self.assertEqual(match["id"], app["id"])
        self.assertTrue(store.launch({"id": match["id"]})["launched"])
        self.assertEqual(len(launched), 2)
        self.assertEqual(
            self._store(launcher=launch, native_host=self._native_host("")).search({"query": "calculator"}),
            [],
        )
        self.assertTrue(store.publish({"id": app["id"], "summary": "Calculator", "keywords": ["calculator"]})["published"])

    def test_draft_launch_rejects_missing_html_and_unexpected_files(self):
        launcher = mock.Mock()
        store = self._store(launcher=launcher)
        app = store.create({"title": "Invalid", "request": "Create an app"})
        with self.assertRaisesRegex(ValueError, "Write the HTML"):
            store.launch({"id": app["id"]})
        store.write({"id": app["id"], "html": "<!doctype html><title>Draft</title>"})
        (self.root / app["id"] / "extra.js").write_text("invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            store.launch({"id": app["id"]})
        launcher.assert_not_called()

    def test_failed_draft_launch_removes_snapshot_and_preserves_draft(self):
        folders = []
        store = self._store(launcher=lambda folder: folders.append(folder) or False)
        app = store.create({"title": "Fail", "request": "Create a failing app"})
        store.write({"id": app["id"], "html": "<!doctype html><title>Draft</title>"})
        result = store.launch({"id": app["id"]})
        self.assertFalse(result["launched"])
        self.assertEqual(result["reason"], applications.LAUNCH_FAILURE_REASON)
        self.assertFalse(folders[0].parent.exists())
        self.assertTrue((self.root / app["id"] / ".draft.json").exists())

    @unittest.skipUnless(os.name == "posix", "chat-owned process groups require POSIX")
    def test_chat_close_reaps_native_and_web_apps_without_affecting_other_chat(self):
        from aios.toolhost import ToolHost

        host_binary = self._native_host("""
            import os, time
            os.write(int(os.environ["AIOS_APP_READY_FD"]), b"ready\\n")
            os.close(int(os.environ["AIOS_APP_READY_FD"]))
            time.sleep(30)
        """)
        chromium = self._write_executable("aios-browser", """
            import os, sys, time
            from urllib.request import urlopen
            url = sys.argv[sys.argv.index("--url") + 1]
            urlopen(url, timeout=5).read()
            time.sleep(30)
        """)
        env = {
            "PATH": str(chromium.parent) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "apps"),
        }
        stores = [self._store(native_host=host_binary, launch_timeout=5) for _ in range(2)]
        hosts = [ToolHost(browser=mock.Mock(), applications=store, mcp=mock.Mock()) for store in stores]
        for host in hosts:
            self.addCleanup(host.close)
        with mock.patch.dict(os.environ, env):
            for runtime in ("native", "web"):
                payload = {"title": "Owned", "request": "Create an owned app", "runtime": runtime}
                if runtime == "native":
                    payload["template"] = "calculator"
                app = stores[0].create(payload)
                if runtime == "web":
                    stores[0].write({"id": app["id"], "html": "<!doctype html><title>Owned</title>"})
                for host in hosts:
                    self.assertTrue(host.call("application", {"action": "launch", "id": app["id"]})["launched"])
        children = [list(store._processes.items()) for store in stores]
        for group in children:
            self.assertEqual(len(group), 2)
            for process, snapshot in group:
                self.assertIsNone(process.poll())
                self.assertTrue(Path(snapshot.name).exists())
        hosts[0].close()
        for process, snapshot in children[0]:
            self.assertIsNotNone(process.poll())
            self.assertFalse(Path(snapshot.name).exists())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
        for process, snapshot in children[1]:
            self.assertIsNone(process.poll())
            self.assertTrue(Path(snapshot.name).exists())
        hosts[1].close()
        for process, snapshot in children[1]:
            self.assertIsNotNone(process.poll())
            self.assertFalse(Path(snapshot.name).exists())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            stores[0].launch({"id": app["id"]})
        self.assertTrue((self.root / app["id"] / ".draft.json").exists())

    @unittest.skipUnless(os.name == "posix", "launch and shutdown races require POSIX")
    def test_close_during_spawn_or_readiness_cannot_orphan_app(self):
        host = self._native_host("import time\ntime.sleep(30)\n")
        real_popen = subprocess.Popen
        for stage in ("spawn", "readiness"):
            with self.subTest(stage=stage):
                store = self._store(native_host=host, launch_timeout=30)
                app = store.create({
                    "title": "Race", "request": "Create a calculator",
                    "runtime": "native", "template": "calculator",
                })
                spawned = Event()
                proceed = Event()
                processes = []
                results = []

                def popen(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    spawned.set()
                    if stage == "spawn":
                        self.assertTrue(proceed.wait(5))
                    return process

                with mock.patch.object(applications.subprocess, "Popen", side_effect=popen):
                    launch = Thread(target=lambda: results.append(store.launch({"id": app["id"]})))
                    launch.start()
                    self.assertTrue(spawned.wait(5))
                    close = Thread(target=store.close)
                    close.start()
                    self.assertTrue(store._closed.wait(2))
                    proceed.set()
                    launch.join(5)
                    close.join(5)
                self.assertFalse(launch.is_alive())
                self.assertFalse(close.is_alive())
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0]["launched"])
                self.assertIsNotNone(processes[0].poll())
                self.assertEqual(store._processes, {})

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
        self.assertEqual(launch_result, {
            "launched": True,
            "id": created["id"],
            "title": "Hello World",
            "runtime": "web",
        })
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
        self.assertEqual(launch_result, {
            "launched": True,
            "id": created["id"],
            "title": "Recover",
            "runtime": "web",
        })
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

    def test_title_rejects_c0_and_del_characters(self):
        store = self._store()
        for title in ("Hello\nWorld", "Hello\tWorld", "Hello\x1fWorld", "Hello\x7fWorld"):
            with self.subTest(title=repr(title)):
                with self.assertRaises(ValueError):
                    store.create({"title": title, "request": "Make a title-safe page"})

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
        result = store.launch({"id": created["id"]})
        self.assertEqual(result, {
            "launched": False,
            "id": created["id"],
            "title": "Keep",
            "runtime": "web",
            "reason": "Application window could not open.",
        })
        self.assertEqual((self.root / created["id"] / "manifest.json").read_text(encoding="utf-8"), manifest)
        self.assertTrue((self.root / created["id"] / "index.html").exists())

    def test_native_create_publish_search_launch_and_reuse(self):
        host = self._native_host("")
        launched = []
        store = self._store(
            launcher=lambda folder: launched.append(Path(folder)),
            native_host=host,
            native_templates=("calculator",),
        )
        created = store.create({
            "title": "Calculator",
            "request": "Build a calculator",
            "runtime": "native",
            "template": "calculator",
        })
        self.assertEqual(created["runtime"], "native")
        self.assertEqual(created["template"], "calculator")
        with self.assertRaisesRegex(ValueError, "cannot accept HTML"):
            store.write({"id": created["id"], "html": "<!doctype html>"})
        with self.assertRaisesRegex(ValueError, "do not have"):
            store.read({"id": created["id"]})

        published = store.publish({
            "id": created["id"],
            "summary": "Trusted calculator",
            "keywords": ["calculator"],
        })
        self.assertEqual(published, {
            "published": True,
            "id": created["id"],
            "runtime": "native",
            "template": "calculator",
        })
        folder = self.root / created["id"]
        self.assertEqual({path.name for path in folder.iterdir()}, {"manifest.json"})
        manifest = _read_json(folder / "manifest.json")
        self.assertEqual(manifest["runtime"], "native")
        self.assertEqual(manifest["template"], "calculator")
        self.assertNotIn("entrypoint", manifest)
        self.assertNotIn("sha256", manifest)
        with self.assertRaisesRegex(ValueError, "do not have"):
            store.read({"id": created["id"]})

        matches = store.search({"query": "Build a calculator"})
        self.assertEqual(matches[0]["runtime"], "native")
        self.assertEqual(matches[0]["template"], "calculator")
        launch = store.launch({"id": matches[0]["id"]})
        self.assertEqual(launch, {
            "launched": True,
            "id": created["id"],
            "title": "Calculator",
            "runtime": "native",
            "template": "calculator",
        })
        self.assertEqual(launched, [folder])

    def test_native_interrupted_publish_recovery_self_heals_for_search_and_launch(self):
        host = self._native_host("")
        launched = []
        store = self._store(
            launcher=lambda folder: launched.append(Path(folder)),
            native_host=host,
            native_templates=("calculator",),
        )
        created = store.create({
            "title": "Calculator",
            "request": "Recover native calculator",
            "runtime": "native",
            "template": "calculator",
        })
        store.publish({"id": created["id"], "summary": "Recovered", "keywords": ["calculator", "recover"]})
        folder = self.root / created["id"]
        manifest = _read_json(folder / "manifest.json")
        draft_path = folder / ".draft.json"

        draft_path.write_text(json.dumps({
            "id": created["id"],
            "title": "Calculator",
            "request": "Recover native calculator",
            "created_at": manifest["created_at"],
            "runtime": "native",
            "template": "calculator",
        }, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        results = store.search({"query": "Recover native calculator"})
        self.assertEqual(results[0]["id"], created["id"])
        self.assertEqual(results[0]["runtime"], "native")
        self.assertFalse(draft_path.exists())

        draft_path.write_text(json.dumps({
            "id": created["id"],
            "title": "Calculator",
            "request": "Recover native calculator",
            "created_at": manifest["created_at"],
            "runtime": "native",
            "template": "calculator",
        }, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        launch = store.launch({"id": created["id"]})
        self.assertEqual(launch, {
            "launched": True,
            "id": created["id"],
            "title": "Calculator",
            "runtime": "native",
            "template": "calculator",
        })
        self.assertEqual(launched, [folder])
        self.assertFalse(draft_path.exists())

    def test_native_interrupted_publish_recovery_rejects_mismatched_leftover_draft(self):
        host = self._native_host("")
        launched = []
        store = self._store(
            launcher=lambda folder: launched.append(Path(folder)),
            native_host=host,
            native_templates=("calculator",),
        )
        created = store.create({
            "title": "Calculator",
            "request": "Reject native calculator recovery",
            "runtime": "native",
            "template": "calculator",
        })
        store.publish({"id": created["id"], "summary": "Rejected", "keywords": ["calculator", "reject"]})
        folder = self.root / created["id"]
        manifest = _read_json(folder / "manifest.json")
        draft_path = folder / ".draft.json"
        draft_path.write_text(json.dumps({
            "id": created["id"],
            "title": "Calculator",
            "request": "Wrong request",
            "created_at": manifest["created_at"],
            "runtime": "native",
            "template": "calculator",
        }, separators=(",", ":"), sort_keys=True), encoding="utf-8")

        self.assertEqual(store.search({"query": "Reject native calculator recovery"}), [])
        with self.assertRaises(ValueError):
            store.launch({"id": created["id"]})
        self.assertTrue(draft_path.exists())
        self.assertEqual(launched, [])

    def test_legacy_web_manifest_loads_and_native_wins_exact_tie(self):
        host = self._native_host("")
        store = self._store(launcher=lambda folder: None, native_host=host, native_templates=NATIVE_TEMPLATES)
        web = store.create({"title": "Calculator", "request": "Build calculator"})
        store.write({"id": web["id"], "html": "<!doctype html><p>web</p>"})
        store.publish({"id": web["id"], "summary": "Calculator", "keywords": ["calculator"]})
        web_manifest_path = self.root / web["id"] / "manifest.json"
        web_manifest = _read_json(web_manifest_path)
        web_manifest.pop("runtime")
        web_manifest["updated_at"] = "2026-01-01T00:00:00Z"
        web_manifest_path.write_text(json.dumps(web_manifest, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        self.assertEqual(store.read({"id": web["id"]}), "<!doctype html><p>web</p>")

        native = store.create({
            "title": "Calculator",
            "request": "Build calculator",
            "runtime": "native",
            "template": "calculator",
        })
        store.publish({"id": native["id"], "summary": "Calculator", "keywords": ["calculator"]})
        native_manifest_path = self.root / native["id"] / "manifest.json"
        native_manifest = _read_json(native_manifest_path)
        native_manifest["updated_at"] = "2026-01-01T00:00:00Z"
        native_manifest_path.write_text(json.dumps(native_manifest, separators=(",", ":"), sort_keys=True), encoding="utf-8")

        results = store.search({"query": "Build calculator"})
        self.assertEqual([result["runtime"] for result in results[:2]], ["native", "web"])
        self.assertIsNone(results[1]["template"])

    def test_search_prefers_native_before_web_then_recency_within_runtime(self):
        host = self._native_host("")
        store = self._store(launcher=lambda folder: None, native_host=host, native_templates=NATIVE_TEMPLATES)
        created = []
        for runtime, template in [
            ("web", None),
            ("web", None),
            ("native", "calculator"),
            ("native", "calculator"),
        ]:
            payload = {
                "title": "Calculator",
                "request": "Build a shared calculator",
                "runtime": runtime,
            }
            if template is not None:
                payload["template"] = template
            app = store.create(payload)
            if runtime == "web":
                store.write({"id": app["id"], "html": f"<!doctype html><p>{app['id']}</p>"})
            store.publish({"id": app["id"], "summary": "Calculator", "keywords": ["calculator", "shared"]})
            created.append((runtime, app["id"]))

        timestamps = {
            created[0][1]: "2026-01-04T00:00:00Z",
            created[1][1]: "2026-01-03T00:00:00Z",
            created[2][1]: "2026-01-02T00:00:00Z",
            created[3][1]: "2026-01-05T00:00:00Z",
        }
        for _runtime, app_id in created:
            manifest_path = self.root / app_id / "manifest.json"
            manifest = _read_json(manifest_path)
            manifest["updated_at"] = timestamps[app_id]
            manifest_path.write_text(json.dumps(manifest, separators=(",", ":"), sort_keys=True), encoding="utf-8")

        results = store.search({"query": "Build a shared calculator"})
        self.assertEqual(
            [result["id"] for result in results[:4]],
            [created[3][1], created[2][1], created[0][1], created[1][1]],
        )
        self.assertEqual(
            [result["runtime"] for result in results[:4]],
            ["native", "native", "web", "web"],
        )

    def test_invalid_or_unavailable_native_create_is_rejected(self):
        with mock.patch.dict(os.environ, {"AIOS_APP_HOST": ""}), \
                mock.patch.object(applications, "_is_regular_file", return_value=False):
            store = self._store()
        self.assertNotIn("runtime", store.definition()["function"]["parameters"]["properties"])
        for payload in (
            {"title": "Bad", "request": "Bad", "runtime": "desktop"},
            {"title": "Bad", "request": "Bad", "runtime": "native", "template": "calculator"},
            {"title": "Bad", "request": "Bad", "runtime": "web", "template": "calculator"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.create(payload)
        with self.assertRaises(ValueError):
            ApplicationStore(root=Path(self.tmp.name) / "other", native_host=None, native_templates=("calculator",))
        host = self._native_host("")
        with self.assertRaises(ValueError):
            ApplicationStore(root=Path(self.tmp.name) / "third", native_host=host, native_templates=("unknown",))

    def test_default_native_host_uses_environment_override(self):
        host = self._native_host("")
        with mock.patch.dict(os.environ, {"AIOS_APP_HOST": str(host)}):
            store = self._store()
        self.assertEqual(store.native_host, host)
        self.assertEqual(store.native_templates, NATIVE_TEMPLATES)
        self.assertIn("runtime", store.definition()["function"]["parameters"]["properties"])

    def test_default_native_host_path_absent_remains_web_only(self):
        with mock.patch.dict(os.environ, {"AIOS_APP_HOST": ""}), \
                mock.patch.object(applications, "_is_regular_file", return_value=False) as regular_file:
            store = self._store()
        self.assertEqual(store.native_host, Path("/usr/local/bin/aios-app-host"))
        self.assertEqual(store.native_templates, ())
        self.assertNotIn("runtime", store.definition()["function"]["parameters"]["properties"])
        regular_file.assert_called_once_with(Path("/usr/local/bin/aios-app-host"))

    def test_default_native_host_rejects_symlink_override(self):
        host = self._native_host("")
        link = Path(self.tmp.name) / "host-link"
        try:
            link.symlink_to(host)
        except (OSError, NotImplementedError):
            self.skipTest("environment cannot create symlinks")
        with mock.patch.dict(os.environ, {"AIOS_APP_HOST": str(link)}):
            store = self._store()
        self.assertEqual(store.native_host, link)
        self.assertEqual(store.native_templates, ())

    def test_injected_launcher_false_and_exception_are_structured(self):
        for launcher in (lambda _folder: False, lambda _folder: (_ for _ in ()).throw(RuntimeError("secret"))):
            with self.subTest(launcher=launcher):
                root = Path(self.tmp.name) / f"applications-{id(launcher)}"
                store = ApplicationStore(root=root, launcher=launcher)
                created = store.create({"title": "Fail", "request": "Fail safely"})
                store.write({"id": created["id"], "html": "<!doctype html><p>fail</p>"})
                store.publish({"id": created["id"], "summary": "Failure", "keywords": ["failure"]})
                result = store.launch({"id": created["id"]})
                self.assertFalse(result["launched"])
                self.assertEqual(result["reason"], "Application window could not open.")
                self.assertNotIn(str(root), json.dumps(result))

    @unittest.skipUnless(os.name == "posix", "verified web default launcher requires POSIX")
    def test_real_web_default_launcher_signals_ready_and_uses_fixed_command(self):
        capture = Path(self.tmp.name) / "chromium-ready.json"
        chromium = self._write_executable("aios-browser", """
            import json
            import os
            import sys
            from urllib.request import urlopen

            app_url = sys.argv[sys.argv.index("--url") + 1]
            base_url = app_url.rsplit("/app", 1)[0]
            wrapper = urlopen(base_url + "/", timeout=5).read().decode("utf-8")
            app = urlopen(app_url, timeout=5).read().decode("utf-8")
            with open(os.environ["AIOS_TEST_CAPTURE"], "w", encoding="utf-8") as stream:
                json.dump({"argv": sys.argv[1:], "wrapper": wrapper, "app": app, "pid": os.getpid()}, stream)
        """)
        popen_calls = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            return real_popen(*args, **kwargs)

        store = self._store(launch_timeout=2)
        created = store.create({"title": "Web Launch", "request": "Launch a web app"})
        html = "<!doctype html><title>Launch</title><p>ready</p>"
        store.write({"id": created["id"], "html": html})
        store.publish({"id": created["id"], "summary": "Launch", "keywords": ["launch", "web"]})
        folder = self.root / created["id"]
        env = {
            "PATH": str(chromium.parent) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "apps") + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "AIOS_TEST_CAPTURE": str(capture),
        }

        with mock.patch.dict(os.environ, env, clear=False), \
            mock.patch.object(applications.subprocess, "Popen", side_effect=recording_popen):
            result = store.launch({"id": created["id"]})

        self.assertTrue(result["launched"])
        self._wait_for_path(capture)
        captured = _read_json(capture)
        self.assertIn('<iframe sandbox="allow-scripts" src="/app"', captured["wrapper"])
        self.assertEqual(captured["app"], html)
        command = popen_calls[0][0][0]
        self.assertEqual(command[:5], [sys.executable, "-m", "aios.app_runner", str(folder), "--ready-fd"])
        self.assertRegex(command[5], r"^\d+$")

    @unittest.skipUnless(os.name == "posix", "verified web default launcher requires POSIX")
    def test_real_web_default_launcher_times_out_and_reaps_runner_tree(self):
        capture = Path(self.tmp.name) / "chromium-timeout.json"
        chromium = self._write_executable("aios-browser", """
            import json
            import os
            import signal
            import sys
            import time
            from urllib.request import urlopen

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            app_url = sys.argv[sys.argv.index("--url") + 1]
            base_url = app_url.rsplit("/app", 1)[0]
            with open(os.environ["AIOS_TEST_CAPTURE"], "w", encoding="utf-8") as stream:
                json.dump({"argv": sys.argv[1:], "pid": os.getpid()}, stream)
            urlopen(base_url + "/", timeout=5).read()
            time.sleep(30)
        """)
        popen_calls = []
        runner_pids = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            popen_calls.append((args, kwargs))
            runner_pids.append(process.pid)
            return process

        store = self._store(launch_timeout=1)
        created = store.create({"title": "Web Timeout", "request": "Launch a hanging web app"})
        store.write({"id": created["id"], "html": "<!doctype html><p>hang</p>"})
        store.publish({"id": created["id"], "summary": "Hang", "keywords": ["launch", "hang"]})
        env = {
            "PATH": str(chromium.parent) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "apps") + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "AIOS_TEST_CAPTURE": str(capture),
        }

        started = time.monotonic()
        with mock.patch.dict(os.environ, env, clear=False), \
            mock.patch.object(applications.subprocess, "Popen", side_effect=recording_popen):
            result = store.launch({"id": created["id"]})
        elapsed = time.monotonic() - started

        self.assertEqual(result["launched"], False)
        self.assertEqual(result["reason"], applications.LAUNCH_FAILURE_REASON)
        self.assertLess(elapsed, 4)
        self._wait_for_path(capture)
        captured = _read_json(capture)
        command = popen_calls[0][0][0]
        self.assertEqual(command[:5], [sys.executable, "-m", "aios.app_runner", str(self.root / created["id"]), "--ready-fd"])
        self.assertRegex(command[5], r"^\d+$")
        self._wait_for_pid_exit(runner_pids[0], timeout=5)
        self._wait_for_pid_exit(captured["pid"], timeout=5)

    def test_default_launcher_readiness_exceptions_terminate_child_and_return_false(self):
        class RaisingSelector:
            def __init__(self, read_should_raise=False):
                self._read_should_raise = read_should_raise

            def register(self, *_args, **_kwargs):
                return None

            def select(self, *_args, **_kwargs):
                if self._read_should_raise:
                    return [object()]
                raise RuntimeError("selector exploded")

            def close(self):
                return None

        for mode in ("selector", "read"):
            with self.subTest(mode=mode):
                root = Path(self.tmp.name) / mode
                store = ApplicationStore(root=root, launch_timeout=0.1)
                created = store.create({"title": f"Web {mode}", "request": f"Launch {mode}"})
                store.write({"id": created["id"], "html": "<!doctype html><p>boom</p>"})
                store.publish({"id": created["id"], "summary": "Boom", "keywords": ["boom"]})

                proc = mock.Mock()
                proc.pid = 4321
                proc.poll.return_value = None
                proc.wait.return_value = 0
                selector = RaisingSelector(read_should_raise=mode == "read")
                read_mock = mock.Mock(side_effect=RuntimeError("read exploded") if mode == "read" else AssertionError("unexpected read"))

                with mock.patch.object(applications.os, "name", "posix", create=True), \
                    mock.patch.object(applications.subprocess, "Popen", return_value=proc), \
                    mock.patch.object(applications.selectors, "DefaultSelector", return_value=selector), \
                    mock.patch.object(applications.os, "killpg", create=True) as killpg, \
                    mock.patch.object(applications.os, "read", read_mock):
                    result = store.launch({"id": created["id"]})

                self.assertEqual(result["launched"], False)
                self.assertEqual(result["reason"], applications.LAUNCH_FAILURE_REASON)
                self.assertEqual(killpg.mock_calls, [
                    mock.call(proc.pid, signal.SIGTERM),
                    mock.call(proc.pid, signal.SIGKILL),
                ])
                proc.wait.assert_called()

    def test_ready_message_from_exited_process_is_not_launch_success(self):
        store = self._store(launch_timeout=1)
        app = store.create({"title": "Exited", "request": "Create an app"})
        store.write({"id": app["id"], "html": "<!doctype html><title>Exited</title>"})
        process = mock.Mock(pid=4321)
        process.poll.return_value = 1
        selector = mock.Mock()
        selector.select.return_value = [object()]
        with mock.patch.object(applications.os, "name", "posix"), \
                mock.patch.object(applications.subprocess, "Popen", return_value=process), \
                mock.patch.object(applications.selectors, "DefaultSelector", return_value=selector), \
                mock.patch.object(applications.os, "read", return_value=b"ready\n"), \
                mock.patch.object(applications.os, "killpg", create=True):
            result = store.launch({"id": app["id"]})
        self.assertFalse(result["launched"])
        self.assertEqual(result["reason"], applications.LAUNCH_FAILURE_REASON)
        self.assertEqual(store._processes, {})

    def test_default_launcher_cancellation_and_reaper_failure_terminate_child(self):
        class Selector:
            def __init__(self, error=None):
                self.error = error

            def register(self, *_args, **_kwargs):
                return None

            def select(self, *_args, **_kwargs):
                if self.error is not None:
                    raise self.error
                return [object()]

            def close(self):
                return None

        for mode in ("cancelled", "reaper"):
            with self.subTest(mode=mode):
                root = Path(self.tmp.name) / mode
                store = ApplicationStore(root=root, launch_timeout=0.1)
                created = store.create({"title": f"Web {mode}", "request": f"Launch {mode}"})
                store.write({"id": created["id"], "html": "<!doctype html><p>stop</p>"})
                store.publish({"id": created["id"], "summary": "Stop", "keywords": ["stop"]})

                proc = mock.Mock()
                proc.pid = 9876
                proc.poll.return_value = None
                proc.wait.return_value = 0
                selector = Selector(SystemExit(143) if mode == "cancelled" else None)
                thread = mock.Mock()
                if mode == "reaper":
                    thread.start.side_effect = RuntimeError("thread unavailable")

                with mock.patch.object(applications.os, "name", "posix", create=True), \
                    mock.patch.object(applications.subprocess, "Popen", return_value=proc), \
                    mock.patch.object(applications.selectors, "DefaultSelector", return_value=selector), \
                    mock.patch.object(applications.threading, "Thread", return_value=thread), \
                    mock.patch.object(applications.os, "killpg", create=True) as killpg, \
                    mock.patch.object(applications.os, "read", return_value=b"ready\n"):
                    if mode == "cancelled":
                        with self.assertRaisesRegex(SystemExit, "143"):
                            store.launch({"id": created["id"]})
                    else:
                        result = store.launch({"id": created["id"]})
                        self.assertFalse(result["launched"])

                self.assertEqual(killpg.mock_calls, [
                    mock.call(proc.pid, signal.SIGTERM),
                    mock.call(proc.pid, signal.SIGKILL),
                ])
                proc.wait.assert_called()

    @unittest.skipUnless(os.name == "posix", "verified fd launch requires POSIX")
    def test_verified_native_launcher_ready_exit_and_timeout(self):
        cases = {
            "ready": """
                import os, time
                os.write(int(os.environ["AIOS_APP_READY_FD"]), b"ready\\n")
                os.close(int(os.environ["AIOS_APP_READY_FD"]))
                time.sleep(30)
            """,
            "exit": "pass\n",
            "timeout": """
                import os, time
                with open(os.environ["AIOS_TEST_PID_FILE"], "w", encoding="utf-8") as stream:
                    stream.write(str(os.getpid()))
                time.sleep(10)
            """,
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                root = Path(self.tmp.name) / name
                host = self._native_host(body)
                pid_file = Path(self.tmp.name) / f"{name}.pid"
                with mock.patch.dict(os.environ, {"AIOS_TEST_PID_FILE": str(pid_file)}):
                    store = ApplicationStore(
                        root=root,
                        native_host=host,
                        native_templates=("calculator",),
                        launch_timeout=0.75,
                    )
                    created = store.create({
                        "title": "Calculator",
                        "request": f"Calculator {name}",
                        "runtime": "native",
                        "template": "calculator",
                    })
                    store.publish({"id": created["id"], "summary": "Calculator", "keywords": ["calculator"]})
                    started = time.monotonic()
                    result = store.launch({"id": created["id"]})
                    elapsed = time.monotonic() - started
                self.assertEqual(result["launched"], name == "ready")
                self.assertLess(elapsed, 2)
                store.close()
                if name == "timeout":
                    pid = int(pid_file.read_text(encoding="utf-8"))
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    @unittest.skipUnless(os.name == "posix", "verified fd launch requires POSIX")
    def test_native_launcher_uses_fixed_argv_and_environment(self):
        capture = Path(self.tmp.name) / "capture.json"
        host = self._native_host("""
            import json, os, sys, time
            with open(os.environ["AIOS_TEST_CAPTURE"], "w", encoding="utf-8") as stream:
                json.dump({
                    "argv": sys.argv,
                    "template": os.environ["AIOS_APP_TEMPLATE"],
                    "title": os.environ["AIOS_APP_TITLE"],
                }, stream)
            os.write(int(os.environ["AIOS_APP_READY_FD"]), b"ready\\n")
            os.close(int(os.environ["AIOS_APP_READY_FD"]))
            time.sleep(30)
        """)
        with mock.patch.dict(os.environ, {"AIOS_TEST_CAPTURE": str(capture)}):
            store = self._store(native_host=host, native_templates=("calculator",))
            created = store.create({
                "title": "Calculator --bad",
                "request": "Native calculator",
                "runtime": "native",
                "template": "calculator",
            })
            store.publish({"id": created["id"], "summary": "Calculator", "keywords": ["calculator"]})
            self.assertTrue(store.launch({"id": created["id"]})["launched"])
        captured = _read_json(capture)
        self.assertEqual(captured["argv"], [str(host)])
        self.assertEqual(captured["template"], "calculator")
        self.assertEqual(captured["title"], "Calculator --bad")
        capture.unlink()
        manifest_path = self.root / created["id"] / "manifest.json"
        manifest = _read_json(manifest_path)
        manifest["template"] = "unsupported"
        manifest_path.write_text(json.dumps(manifest, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        with self.assertRaises(ValueError):
            store.launch({"id": created["id"]})
        self.assertFalse(capture.exists())

    @unittest.skipUnless(os.name == "posix", "compiled native host validation requires POSIX")
    def test_compiled_native_host_binary_ready_protocol_and_validation(self):
        binary = os.environ.get("AIOS_APP_HOST_TEST_BINARY")
        if not binary:
            self.skipTest("AIOS_APP_HOST_TEST_BINARY not set")
        host = Path(binary)
        self.assertTrue(host.is_file(), binary)
        secret = "native-host-secret-value"

        def run_host(*, template="calculator", title="Calculator"):
            read_fd, write_fd = os.pipe()
            env = os.environ.copy()
            env.update({
                "AIOS_APP_TEMPLATE": template,
                "AIOS_APP_TITLE": title,
                "AIOS_APP_READY_FD": str(write_fd),
                "AIOS_TEST_SECRET": secret,
                "QT_QPA_PLATFORM": "offscreen",
                "QT_QUICK_BACKEND": "software",
            })
            process = None
            try:
                process = subprocess.Popen(
                    [str(host)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                    pass_fds=(write_fd,),
                    env=env,
                )
            finally:
                os.close(write_fd)

            ready = b""
            selector = selectors.DefaultSelector()
            try:
                selector.register(read_fd, selectors.EVENT_READ)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    events = selector.select(max(0, deadline - time.monotonic()))
                    if not events:
                        break
                    chunk = os.read(read_fd, 64)
                    if not chunk:
                        break
                    ready += chunk
            finally:
                selector.close()
                os.close(read_fd)

            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            return process.returncode, ready, stdout, stderr

        returncode, ready, stdout, stderr = run_host()
        self.assertEqual(ready, b"ready\n")
        self.assertEqual(stdout, b"")
        self.assertNotIn(secret, (stdout + stderr).decode("utf-8", errors="replace"))

        for kwargs in (
            {"template": "unsupported"},
            {"title": "Bad\x7fTitle"},
        ):
            with self.subTest(kwargs=kwargs):
                returncode, ready, stdout, stderr = run_host(**kwargs)
                self.assertNotEqual(returncode, 0)
                self.assertEqual(ready, b"")
                self.assertEqual(stdout, b"")
                self.assertIn(b"startup failed", stderr)
                self.assertNotIn(secret, (stdout + stderr).decode("utf-8", errors="replace"))

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

    def test_handler_signals_once_only_after_get_app_body(self):
        from aios import app_runner

        callbacks = []
        ready = Event()

        def on_app_loaded():
            callbacks.append("ready")
            ready.set()

        handler = app_runner.handler_for(
            "<!doctype html><p>ready</p>",
            on_app_loaded=on_app_loaded,
        )
        with self._serve(handler) as base_url:
            self._get(base_url + "/")
            self._get(base_url + "/missing", expect_error=True)
            self._get(base_url + "/app", method="HEAD")
            self.assertFalse(ready.is_set())
            app = self._get(base_url + "/app")
            self.assertEqual(app["body"], b"<!doctype html><p>ready</p>")
            self.assertTrue(ready.wait(timeout=1))
            self.assertEqual(callbacks, ["ready"])
            self._get(base_url + "/app")
            self.assertEqual(callbacks, ["ready"])

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

    def test_load_document_rejects_invalid_manifest_timestamps(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Timestamp", "request": "Build a timestamp page"})
        html = "<!doctype html><p>timestamp</p>"
        store.write({"id": created["id"], "html": html})
        store.publish({"id": created["id"], "summary": "Timestamp summary", "keywords": ["timestamp"]})
        folder = self.root / created["id"]
        manifest_path = folder / "manifest.json"
        manifest = _read_json(manifest_path)
        original_manifest = dict(manifest)

        for field in ["created_at", "updated_at"]:
            with self.subTest(field=field):
                manifest = dict(original_manifest)
                manifest[field] = "NOT-A-DATE"
                manifest_path.write_text(json.dumps(manifest, separators=(",", ":"), sort_keys=True), encoding="utf-8")
                with self.assertRaises(ValueError):
                    app_runner.load_document(folder)

    def test_run_uses_themed_browser_webview(self):
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
        self.assertEqual(args[0], "aios-browser")
        self.assertEqual(
            args[args.index("--url") + 1],
            f"http://127.0.0.1:{state['server'].server_address[1]}/app",
        )
        self.assertEqual(args[args.index("--theme") + 1], "blue")
        self.assertFalse(popen.call_args.kwargs["start_new_session"])

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

    def test_main_returns_sigterm_code_and_runs_cleanup_without_traceback(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Signal", "request": "Build a signal page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>signal</p>"})
        store.publish({"id": created["id"], "summary": "Signal summary", "keywords": ["signal"]})
        folder = self.root / created["id"]

        state, server_class = self._fake_server_class()
        cleanup = {"pids": []}
        installed = {}

        previous_sigterm = signal.getsignal(signal.SIGTERM)
        previous_sighup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None
        proc = mock.Mock()
        proc.pid = 8765
        proc.poll.return_value = None

        def wait_side_effect(timeout=None):
            if timeout is None:
                installed["sigterm"] = signal.getsignal(signal.SIGTERM)
                if hasattr(signal, "SIGHUP"):
                    installed["sighup"] = signal.getsignal(signal.SIGHUP)
                installed["sigterm"](signal.SIGTERM, None)
            return 0

        proc.wait.side_effect = wait_side_effect

        stderr = io.StringIO()
        with mock.patch.object(app_runner, "ThreadingHTTPServer", server_class), \
            mock.patch.object(app_runner.subprocess, "Popen", return_value=proc), \
            mock.patch.object(app_runner, "_terminate_process", side_effect=lambda process: cleanup["pids"].append(process.pid), create=True), \
            contextlib.redirect_stderr(stderr):
            exit_code = app_runner.main([str(folder)])

        self.assertEqual(exit_code, 143)
        self.assertIn("sigterm", installed)
        self.assertIsNot(installed["sigterm"], previous_sigterm)
        self.assertTrue(callable(installed["sigterm"]))
        if hasattr(signal, "SIGHUP"):
            self.assertIn("sighup", installed)
            self.assertIsNot(installed["sighup"], previous_sighup)
            self.assertTrue(callable(installed["sighup"]))
            self.assertIs(signal.getsignal(signal.SIGHUP), previous_sighup)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_sigterm)
        self.assertEqual(cleanup["pids"], [8765])
        self.assertIsNotNone(state["server"])
        self.assertTrue(state["server"].shutdown_called)
        self.assertTrue(state["server"].server_close_called)
        self.assertTrue(state["server"].joined.is_set())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn(str(folder), stderr.getvalue())

    def test_run_terminates_chromium_after_cleanup_timeout(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Group", "request": "Build a group page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>group</p>"})
        store.publish({"id": created["id"], "summary": "Group summary", "keywords": ["group"]})
        folder = self.root / created["id"]

        state, server_class = self._fake_server_class()
        proc = mock.Mock()
        proc.pid = 2468
        proc.poll.return_value = None
        cleanup_timeouts = []

        def wait_side_effect(timeout=None):
            if timeout is None:
                return 0
            cleanup_timeouts.append(timeout)
            if len(cleanup_timeouts) == 1:
                raise subprocess.TimeoutExpired(cmd="chromium", timeout=timeout)
            return 0

        proc.wait.side_effect = wait_side_effect

        with mock.patch.object(app_runner, "ThreadingHTTPServer", server_class), \
            mock.patch.object(app_runner.subprocess, "Popen", return_value=proc):
            app_runner.run(folder)

        self.assertEqual(len(cleanup_timeouts), 2)
        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()

    def test_run_skips_signals_when_chromium_already_exited(self):
        from aios import app_runner

        store = self._store()
        created = store.create({"title": "Exited", "request": "Build an exited page"})
        store.write({"id": created["id"], "html": "<!doctype html><p>exited</p>"})
        store.publish({"id": created["id"], "summary": "Exited summary", "keywords": ["exited"]})
        folder = self.root / created["id"]

        state, server_class = self._fake_server_class()
        proc = mock.Mock()
        proc.pid = 1357
        proc.poll.return_value = 0
        proc.wait.return_value = 0

        with mock.patch.object(app_runner, "ThreadingHTTPServer", server_class), \
            mock.patch.object(app_runner.subprocess, "Popen", return_value=proc):
            app_runner.run(folder)

        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_main_requires_exactly_one_folder_argument(self):
        from aios import app_runner

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(app_runner.main([]), 0)
            self.assertNotEqual(app_runner.main(["one", "two"]), 0)

    @unittest.skipUnless(os.name == "posix", "fd cleanup requires POSIX")
    def test_main_ready_fd_parsing_and_failure_cleanup(self):
        from aios import app_runner

        self.assertEqual(app_runner._parse_args(["folder"]), (Path("folder"), None))
        self.assertEqual(app_runner._parse_args(["folder", "--ready-fd", "7"]), (Path("folder"), 7))
        for args in (
            ["folder", "--ready-fd"],
            ["folder", "--ready-fd", "bad"],
            ["folder", "--ready-fd", "-1"],
            ["folder", "--ready-fd", "7", "extra"],
        ):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    app_runner._parse_args(args)

        read_fd, write_fd = os.pipe()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app_runner.main(["folder", "--ready-fd", str(write_fd), "extra"]), 2)
            self.assertEqual(os.read(read_fd, 1), b"")
        finally:
            os.close(read_fd)

    def _get(self, url, expect_error=False, method="GET"):
        req = request.Request(url, method=method)
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
