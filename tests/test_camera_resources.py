import unittest
from unittest.mock import MagicMock

from camera_resources import Resources


class ResourceSamplingTests(unittest.TestCase):
    def sample_handles(self, status):
        resource = Resources.__new__(Resources)
        resource.exiting_process_samples = 0
        process = MagicMock()
        entries = {'fd': MagicMock(), 'status': MagicMock()}
        process.__truediv__.side_effect = entries.__getitem__
        entries['fd'].iterdir.side_effect = PermissionError()
        entries['status'].read_text.return_value = status
        return resource, process

    def test_process_with_no_address_space_is_counted_as_exiting(self):
        resource, process = self.sample_handles('State:\tR (running)\nThreads:\t1\n')
        self.assertEqual(resource.handles(process), [])
        self.assertEqual(resource.exiting_process_samples, 1)

    def test_permission_failure_on_live_process_is_not_suppressed(self):
        resource, process = self.sample_handles('State:\tR (running)\nVmSize:\t8192 kB\n')
        with self.assertRaises(PermissionError):
            resource.handles(process)
        self.assertEqual(resource.exiting_process_samples, 0)


if __name__ == '__main__':
    unittest.main()
