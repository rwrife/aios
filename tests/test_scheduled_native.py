"""Optional compiled Qt bridge against the real scheduler and worker fixture."""
import os
import subprocess
import unittest

import test_scheduler


class ScheduledNativeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('AIOS_SCHEDULED_TEST_BINARY'),
                         'Compiled native scheduler bridge is optional')
    def test_native_configuration_and_history_use_real_service(self):
        fixture = test_scheduler.SchedulerProcessTests()
        fixture.setUpClass()
        self.addCleanup(fixture.tearDownClass)
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        result = subprocess.run(
            [os.environ['AIOS_SCHEDULED_TEST_BINARY'], '--live-service'],
            capture_output=True, text=True, timeout=45, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('real-service binding', result.stdout)


if __name__ == '__main__':
    unittest.main()
