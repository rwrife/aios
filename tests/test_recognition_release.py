import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from aios.recognition_release import GATES, METRICS, assess, digest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def fixtures(self):
        plan = dict(status='approved', review_reference='test-only-review', frozen_at=1,
                    maximums={key: .5 for key in METRICS},
                    minimums=dict(participants=2, genuine_attempts=3, impostor_attempts=4, soak_hours=1))
        for key in ('model_lock_sha256', 'calibration_sha256', 'image_sha256'):
            plan[key] = 'a' * 64
        report = dict(plan_sha256=digest(plan), evaluation_started_at=2,
                      gates={key: dict(status='pass', evidence='unit-test-only') for key in GATES},
                      metrics={key: .1 for key in METRICS}, counts=copy.deepcopy(plan['minimums']))
        report.update({key: plan[key] for key in ('model_lock_sha256', 'calibration_sha256', 'image_sha256')})
        return plan, report

    def test_complete_evidence_only_makes_review_ready_never_approval(self):
        result = assess(*self.fixtures())
        self.assertEqual(result['status'], 'review-ready')
        self.assertFalse(result['runtime_approval_created'])
        self.assertEqual(result['recognition_default'], 'off')

    def test_each_missing_gate_and_metric_blocks(self):
        for section, keys in (('gates', GATES), ('metrics', METRICS)):
            for key in keys:
                plan, report = self.fixtures()
                del report[section][key]
                self.assertEqual(assess(plan, report)['status'], 'blocked', key)

    def test_changed_plan_unapproved_limit_nonfinite_and_insufficient_sample_block(self):
        for change in ('plan', 'unapproved', 'nan', 'limit', 'count', 'freeze'):
            plan, report = self.fixtures()
            if change == 'plan': plan['maximums']['rss_mib'] = 99
            if change == 'unapproved': plan['status'] = 'proposed'
            if change == 'nan': report['metrics']['rss_mib'] = float('nan')
            if change == 'limit': report['metrics']['rss_mib'] = 999
            if change == 'count': report['counts']['impostor_attempts'] = 0
            if change == 'freeze': report['evaluation_started_at'] = 0
            self.assertEqual(assess(plan, report)['status'], 'blocked', change)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('stage_faces', ROOT / 'scripts/stage-face-models.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_corrupt_or_wrong_size_cache_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'weights.onnx'
            path.write_bytes(b'wrong')
            for size in (4, 5):
                with self.assertRaises(ValueError):
                    self.module.verify(path, dict(bytes=size, sha256='0' * 64))

    def test_default_build_removes_optional_model_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / self.module.DESTINATION
            target.mkdir(parents=True)
            (target / 'old.onnx').write_bytes(b'old')
            self.module.stage(root / 'cache', root, False)
            self.assertFalse(target.exists())

    def test_lock_has_pins_licenses_and_no_approval_or_calibration(self):
        lock = json.loads(self.module.LOCK.read_text())
        self.assertNotIn('approval', lock)
        self.assertNotIn('calibration', lock)
        for key in ('yunet', 'sface'):
            record = lock[key]
            self.assertEqual(len(record['sha256']), 64)
            self.assertIn(record['revision'].split(':')[0], record['source'])
            self.assertTrue((ROOT / 'apps/aios/licenses' / record['license_file']).is_file())
