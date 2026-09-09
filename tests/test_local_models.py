import hashlib
import io
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aios import core, local_models as models


class Response(io.BytesIO):
    headers = {}


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target, value in (("XDG_CONFIG_HOME", str(self.root / "config")),
                              ("XDG_DATA_HOME", str(self.root / "data"))):
            p = patch.dict(os.environ, {target: value})
            p.start(); self.addCleanup(p.stop)
        self.weights = b"GGUF test weights"
        self.model = {**models.catalog()["qwen3-4b"], "bytes": len(self.weights),
                      "sha256": hashlib.sha256(self.weights).hexdigest()}
        for target, value in (("catalog", {"qwen3-4b": self.model}),
                              ("memory_bytes", 16 * models.GIB),
                              ("free_disk", 20 * models.GIB)):
            p = patch.object(models, target, return_value=value)
            p.start(); self.addCleanup(p.stop)

    def test_install_verifies_and_switches_then_reuses_offline(self):
        core.save_config({"mode": "remote"})
        with patch("urllib.request.urlopen", return_value=Response(self.weights)) as fetch:
            path = models.install("qwen3-4b")
            self.assertEqual(path.read_bytes(), self.weights)
            self.assertEqual(core.load_config()["mode"], "local")
            self.assertEqual(core.load_config()["model_path"], str(path.resolve()))
            self.assertTrue(models.list_models()["models"][0]["installed"])
            with patch.object(models, "free_disk", return_value=0):
                self.assertEqual(models.install("qwen3-4b"), path)
            self.assertEqual(fetch.call_count, 1)

    def test_insufficient_resources_prevent_download_and_preserve_config(self):
        core.save_config({"mode": "remote"})
        for target, value, message in (("memory_bytes", 4 * models.GIB, "RAM"),
                                       ("free_disk", 0, "disk space")):
            with patch.object(models, target, return_value=value), patch.object(core, "download_model") as fetch:
                self.assertFalse(models.list_models()["models"][0]["available"])
                with self.assertRaisesRegex(ValueError, message):
                    models.install("qwen3-4b")
                fetch.assert_not_called()
                self.assertEqual(core.load_config()["mode"], "remote")

    def test_corrupt_download_and_cached_file_do_not_switch_config(self):
        core.save_config({"mode": "remote"})
        with patch("urllib.request.urlopen", return_value=Response(b"bad")):
            with self.assertRaisesRegex(ValueError, "checksum"):
                models.install("qwen3-4b")
        path = models.destination("qwen3-4b")
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(".gguf.part").exists())
        path.write_bytes(b"corrupt cached model")
        with self.assertRaisesRegex(ValueError, "checksum"):
            models.install("qwen3-4b")
        self.assertEqual(core.load_config()["mode"], "remote")

    def test_cancel_cleans_partial_download_and_preserves_config(self):
        core.save_config({"mode": "remote"})
        def cancel(_):
            raise SystemExit(0)
        with patch("urllib.request.urlopen", return_value=Response(self.weights)):
            with self.assertRaises(SystemExit):
                models.install("qwen3-4b", cancel)
        path = models.destination("qwen3-4b")
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(".gguf.part").exists())
        self.assertEqual(core.load_config()["mode"], "remote")

    def test_unknown_id_cannot_escape_model_directory(self):
        for key in ("../../bad", "unknown", None, []):
            with self.assertRaisesRegex(ValueError, "catalog"):
                models.install(key)

    def test_unknown_memory_and_kernel_reservation(self):
        for ram in (None, int(8 * models.GIB * 0.97)):
            with patch.object(models, "memory_bytes", return_value=ram):
                self.assertTrue(models.list_models()["models"][0]["available"])

    def test_bundled_starter_is_reused(self):
        path = self.root / "starter.gguf"
        path.write_bytes(self.weights)
        with patch.object(core, "BUNDLED_MODEL", path), patch.object(models, "catalog", return_value={models.DEFAULT_MODEL: self.model}), patch.object(core, "download_model") as fetch:
            self.assertEqual(models.install(), path)
            fetch.assert_not_called()


class CatalogTests(unittest.TestCase):
    def test_curated_models_have_pinned_downloads_and_tool_capabilities(self):
        catalog = models.catalog()
        self.assertEqual(models.DEFAULT_MODEL, "qwen3-0.6b")
        self.assertEqual(list(catalog), ["qwen3-0.6b", "qwen3-1.7b", "qwen3-4b", "qwen3-8b"])
        self.assertTrue(catalog[models.DEFAULT_MODEL]["bundled"])
        for model_id, model in catalog.items():
            self.assertRegex(model["url"], r"^https://huggingface.co/.+/resolve/[a-f0-9]{40}/[^/]+\.gguf$")
            self.assertRegex(model["sha256"], r"^[a-f0-9]{64}$")
            self.assertRegex(model["revision"], r"^[a-f0-9]{40}$")
            self.assertIn("/Qwen/Qwen3-", model["source"])
            self.assertEqual(model["quantization"], "Q4_K_M")
            self.assertTrue(model["url"].endswith("/" + model["filename"]))
            self.assertIn("/resolve/" + model["revision"] + "/", model["url"])
            self.assertGreater(model["bytes"], 0)
            self.assertGreater(model["ram_gib"], 0, model_id)
            self.assertLessEqual(model["ram_gib"], 32, model_id)
            self.assertTrue(model["tool_use"], model_id)

    def test_worker_returns_catalog_and_rejects_unknown_model(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "XDG_CONFIG_HOME": temp, "XDG_DATA_HOME": temp}
            for action in ("load", "local-models"):
                result = subprocess.run([sys.executable, "-m", "aios.worker"],
                                        input=json.dumps({"action": action}) + "\n",
                                        text=True, capture_output=True, env=env, check=True)
                event = json.loads(result.stdout)
                self.assertEqual(len(event["local_models"]["models"]), 4)
            result = subprocess.run([sys.executable, "-m", "aios.worker"],
                                    input=json.dumps({"action": "setup-local", "model_id": "unknown"}) + "\n",
                                    text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["type"], "error")

    def test_cli_selects_requested_model_and_enables_server_tools(self):
        from aios import cli
        with patch.object(sys, "argv", ["aios-llm", "setup-local", "qwen3-4b"]), patch.object(models, "install") as install, patch("builtins.print"):
            self.assertEqual(cli.main(), 0)
            self.assertEqual(install.call_args.args[0], "qwen3-4b")
        with patch.object(sys, "argv", ["aios-llm", "setup-local"]), patch.object(models, "install") as install, patch("builtins.print"):
            self.assertEqual(cli.main(), 0)
            self.assertEqual(install.call_args.args[0], models.DEFAULT_MODEL)
        with patch.object(sys, "argv", ["aios-llm", "serve"]), patch.object(cli, "load_config", return_value={"model_path": "/model.gguf"}), patch.object(os, "execvp") as launch:
            self.assertEqual(cli.main(), 0)
            args = launch.call_args.args[1]
            self.assertIn("--jinja", args)
            self.assertEqual(args[args.index("--ctx-size") + 1], "8192")
            self.assertFalse(json.loads(args[args.index("--chat-template-kwargs") + 1])["enable_thinking"])
