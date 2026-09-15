"""Stage-3 CPU preflight: the bundled inference binaries never SIGILL silently.

The bundled llama.cpp/whisper.cpp build enables AVX2-era instruction sets, so
every launch path asks the shared helper first. Fixtures inject /proc/cpuinfo
text; no inference binary is executed.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from aios import cpu_features

ROOT = Path(__file__).resolve().parents[1]
MODERN = ("processor\t: 0\nvendor_id\t: GenuineIntel\n"
          "flags\t\t: fpu vme de pse tsc sse4_1 sse4_2 avx avx2 bmi1 bmi2 f16c fma aes\n")
SANDY_BRIDGE = ("processor\t: 0\nvendor_id\t: GenuineIntel\n"
                "flags\t\t: fpu vme de pse tsc sse4_1 sse4_2 avx aes\n")
CORE2 = "processor\t: 0\nflags\t\t: fpu vme de pse tsc sse2 ssse3\n"
AARCH64 = "processor\t: 0\nFeatures\t: fp asimd evtstrm aes\n"


def cpuinfo(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".cpuinfo", delete=False)
    handle.write(text)
    handle.close()
    return Path(handle.name)


class CpuFeatureTests(unittest.TestCase):
    def path(self, text):
        path = cpuinfo(text)
        self.addCleanup(path.unlink)
        return path

    def test_supported_cpu_passes_the_preflight(self):
        state = cpu_features.inference_support(self.path(MODERN))
        self.assertEqual(state["supported"], True)
        self.assertEqual(state["missing"], [])
        self.assertTrue(state["detected"])
        self.assertEqual(state["reason"], "")
        self.assertEqual(cpu_features.ensure_supported(self.path(MODERN))["supported"], True)

    def test_unsupported_cpus_report_exactly_what_is_missing(self):
        for text, missing in ((SANDY_BRIDGE, ["avx2", "bmi2", "f16c", "fma"]),
                              (CORE2, ["sse4_2", "avx", "avx2", "bmi2", "f16c", "fma"])):
            with self.subTest(missing=missing):
                path = self.path(text)
                state = cpu_features.inference_support(path)
                self.assertFalse(state["supported"])
                self.assertEqual(state["missing"], missing)
                with self.assertRaises(RuntimeError) as failure:
                    cpu_features.ensure_supported(path)
                message = str(failure.exception)
                for name in missing:
                    self.assertIn(name, message)
                self.assertIn("desktop", message.lower())

    def test_unreadable_or_foreign_cpuinfo_is_reported_as_undetected(self):
        for path in (Path("/nonexistent/cpuinfo"), self.path(AARCH64)):
            with self.subTest(path=path):
                state = cpu_features.inference_support(path)
                self.assertTrue(state["supported"])
                self.assertFalse(state["detected"])
                self.assertEqual(state["reason"], cpu_features.UNDETECTED)
                cpu_features.ensure_supported(path)

    def test_required_features_match_the_bundled_build(self):
        # scripts/build-apps.sh passes -DGGML_NATIVE=OFF, which leaves ggml's
        # explicit instruction-set options enabled.
        build = (ROOT / "scripts/build-apps.sh").read_text(encoding="utf-8")
        self.assertIn("-DGGML_NATIVE=OFF", build)
        self.assertEqual(cpu_features.REQUIRED_FEATURES,
                         ("sse4_2", "avx", "avx2", "bmi2", "f16c", "fma"))


class LaunchGatingTests(unittest.TestCase):
    """Every path that starts a bundled inference binary preflights first."""

    def unsupported(self):
        return mock.patch.object(cpu_features, "read_flags", return_value={"sse4_2", "avx"})

    def test_server_launch_is_refused_instead_of_crashing(self):
        from aios import cli
        with self.unsupported(), mock.patch.object(cli, "load_config",
                                                   return_value={"model_path": "/models/x.gguf"}), \
                mock.patch.object(cli.os, "execvp") as execvp, \
                mock.patch.object(cli.sys, "argv", ["aios-llm", "serve"]):
            self.assertEqual(cli.main(), 1)
            execvp.assert_not_called()

    def test_local_transcription_and_speech_download_are_refused(self):
        from aios import voice
        with self.unsupported(), \
                mock.patch.object(voice, "load_config", return_value={
                    "voice_mode": "local", "speech_model_path": __file__}), \
                mock.patch.object(voice, "local_command") as command:
            with self.assertRaisesRegex(RuntimeError, "local inference"):
                voice.transcribe(__file__)
            command.assert_not_called()
        with self.unsupported(), mock.patch.object(voice, "download_model") as download:
            with self.assertRaises(RuntimeError):
                voice.setup_local_voice()
            download.assert_not_called()

    def test_model_download_is_refused_and_inventory_explains_why(self):
        from aios import local_models
        with self.unsupported(), mock.patch.object(local_models.core, "download_model") as download:
            with self.assertRaisesRegex(ValueError, "remote model provider"):
                local_models.install("qwen3-0.6b")
            download.assert_not_called()
            inventory = local_models.list_models()
            self.assertFalse(inventory["local_inference"]["supported"])
            self.assertEqual(inventory["local_inference"]["missing"], ["avx2", "bmi2", "f16c", "fma"])
            for entry in inventory["models"]:
                self.assertFalse(entry["available"])
                self.assertIn("local inference", entry["reason"].lower())

    def test_supported_cpu_leaves_the_inventory_available(self):
        from aios import local_models
        with mock.patch.object(cpu_features, "read_flags",
                               return_value=set(cpu_features.REQUIRED_FEATURES)):
            inventory = local_models.list_models()
        self.assertTrue(inventory["local_inference"]["supported"])
        self.assertTrue(any(entry["available"] for entry in inventory["models"]))

    def test_desktop_shell_checks_the_reported_state_before_starting_the_server(self):
        source = (ROOT / "apps/shell/main.cpp").read_text(encoding="utf-8")
        start = source.index("void startLocal()")
        body = source[start:source.index("void run(QJsonObject request)", start)]
        self.assertLess(body.index("local_inference"), body.index('local.start("llama-server"'))
        self.assertIn("return;", body)

    def test_remote_providers_are_not_gated_by_the_cpu(self):
        from aios import local_models
        with self.unsupported():
            # Nothing in the remote path consults the preflight.
            self.assertNotIn("cpu_features", (ROOT / "apps/aios/core.py").read_text(encoding="utf-8"))
            self.assertFalse(local_models.list_models()["local_inference"]["supported"])


class WorkerReportingTests(unittest.TestCase):
    """The desktop learns the limitation through the existing load payload."""

    def test_load_emits_the_local_inference_state(self):
        home = tempfile.TemporaryDirectory(prefix="aios-worker-")
        self.addCleanup(home.cleanup)
        environment = {"PYTHONPATH": str(ROOT / "apps"), "PATH": "/usr/bin:/bin",
                       "HOME": home.name}
        result = subprocess.run(["python3", "-m", "aios.worker"], input='{"action": "load"}\n',
                                text=True, capture_output=True, env=environment, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(loaded["type"], "loaded")
        state = loaded["local_models"]["local_inference"]
        self.assertEqual(sorted(state), ["detected", "missing", "reason", "required", "supported"])


if __name__ == "__main__":
    unittest.main()
