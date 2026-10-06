"""Offline guards for the hosted validation lane (never builds an ISO)."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"  # v7.0.1, node24


class ValidateWorkflowTests(unittest.TestCase):
    def test_all_validation_jobs_use_the_reviewed_immutable_checkout(self):
        workflow = (ROOT / ".github/workflows/validate.yml").read_text()
        references = re.findall(r"^\s*- uses: actions/checkout@([^\s]+)", workflow, re.M)
        self.assertEqual(references, [CHECKOUT_SHA] * 3)
        self.assertEqual(workflow.count("# v7.0.1 (Node 24)"), 3)
        self.assertNotIn("allow-unsafe-pr-checkout", workflow)
