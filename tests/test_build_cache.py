import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BuildCacheTests(unittest.TestCase):
    def test_shell_autogen_cache_is_isolated_per_checkout(self):
        launcher = (ROOT / 'scripts/build-iso-container.sh').read_text()
        container = (ROOT / 'scripts/container-build.sh').read_text()
        apps = (ROOT / 'scripts/build-apps.sh').read_text()
        powershell = (ROOT / 'scripts/build.ps1').read_text()

        self.assertIn("printf '%s' \"$ROOT_DIR\" | cksum", launcher)
        self.assertIn('-e AIOS_BUILD_CACHE_NAMESPACE="$AIOS_BUILD_CACHE_NAMESPACE"', launcher)
        self.assertIn('/build/worktrees/$AIOS_BUILD_CACHE_NAMESPACE/shell', container)
        self.assertIn('export AIOS_SHELL_BUILD_DIR="$shell_build"', container)
        self.assertIn('SHELL_BUILD=${AIOS_SHELL_BUILD_DIR:-$BUILD/shell}', apps)
        self.assertIn('AIOS_BUILD_CACHE_NAMESPACE', powershell)


if __name__ == '__main__':
    unittest.main()
