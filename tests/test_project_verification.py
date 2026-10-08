"""Readiness is not functional evidence; results bind source and examples."""
import copy
import unittest

from aios import project_bundle, project_verification


class ProjectVerificationTests(unittest.TestCase):
    def setUp(self):
        self.source = {'runtime': 'node-web', 'entry': 'app.js',
                       'files': {'app.js': 'first'}}
        self.examples = [{'name': 'sum', 'input': '2+3', 'expected': '5'}]
        self.evidence = {
            'source_sha256': project_bundle.validate(self.source)['sha256'],
            'examples_sha256': project_verification.examples_digest(self.examples),
            'results': [{'name': 'sum', 'status': 'completed', 'observed': '5'}],
        }

    def verify(self, evidence=None):
        return project_verification.verify(self.source, self.examples,
                                           self.evidence if evidence is None else evidence)

    def test_success_requires_observed_output(self):
        result = self.verify()
        self.assertEqual(result['status'], 'tested')
        self.assertEqual(result['examples'], [{'name': 'sum', 'status': 'passed'}])
        self.assertEqual(result['source_sha256'], self.evidence['source_sha256'])
        self.assertEqual(result['examples_sha256'], self.evidence['examples_sha256'])

    def test_blank_readiness_missing_or_failed_results_cannot_pass(self):
        for results in ([], [{'name': 'sum', 'status': 'completed', 'observed': ''}],
                        [{'name': 'sum', 'status': 'completed', 'observed': 'ready'}],
                        [{'name': 'sum', 'status': 'failed', 'observed': '5'}],
                        [{'name': 'sum', 'status': 'not_tested', 'observed': ''}]):
            with self.subTest(results=results):
                result = self.verify({**self.evidence, 'results': results})
                self.assertNotEqual(result['status'], 'tested')
        partial = self.verify({**self.evidence, 'results': []})
        self.assertEqual(partial['status'], 'partially_verified')
        self.assertEqual(partial['examples'][0]['status'], 'not_tested')

    def test_changed_source_or_expectations_invalidates_evidence(self):
        self.source['files']['app.js'] = 'second'
        with self.assertRaises(ValueError):
            self.verify()
        self.source['files']['app.js'] = 'first'
        self.examples[0]['expected'] = '6'
        with self.assertRaises(ValueError):
            self.verify()

    def test_digest_is_order_independent_and_input_bound(self):
        extra = {'name': 'zero', 'input': '0+0', 'expected': '0'}
        original = project_verification.examples_digest(self.examples + [extra])
        self.assertEqual(original, project_verification.examples_digest([extra] + self.examples))
        extra['input'] = '1-1'
        self.assertNotEqual(original, project_verification.examples_digest(self.examples + [extra]))

    def test_rejects_malformed_unbounded_and_duplicate_examples(self):
        for examples in ([], self.examples * 17, self.examples * 2,
                         self.examples + [{'name': 'sum', 'input': '1+1', 'expected': '2'}],
                         [{'name': 'sum', 'input': '2+3', 'expected': 'é' * 2049}],
                         [{'name': 'sum', 'input': '2+3', 'expected': None}],
                         [{'name': [], 'input': '2+3', 'expected': '5'}],
                         True,
                         [{'name': 'sum', 'input': '2+3', 'expected': ''}],
                         [{'name': 'sum', 'input': '2+3', 'expected': '5', 'code': 'pass'}],
                         [{'name': 'sum', 'input': 'x' * 4097, 'expected': '5'}],
                         [{'name': 'sum\n', 'input': '2+3', 'expected': '5'}],
                         [{'name': 'sum', 'input': '\ud800', 'expected': '5'}]):
            with self.subTest(examples=examples), self.assertRaises(ValueError):
                project_verification.examples_digest(examples)

    def test_rejects_unknown_duplicate_and_malformed_observations(self):
        record = self.evidence['results'][0]
        for results in ([record, record], [{**record, 'name': 'other'}],
                        [{**record, 'status': 'ready'}], [{**record, 'status': []}],
                        [{**record, 'observed': None}],
                        [{**record, 'observed': 'x' * 4097}],
                        [{**record, 'error': 'credential'}], True):
            with self.subTest(results=results), self.assertRaises(ValueError):
                self.verify({**self.evidence, 'results': results})
        with self.assertRaises(ValueError):
            self.verify({**self.evidence, 'built': True})

    def test_report_does_not_echo_input_or_diagnostics(self):
        evidence = copy.deepcopy(self.evidence)
        evidence['results'][0].update(status='failed', observed='secret=do-not-log')
        result = self.verify(evidence)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('do-not-log', str(result))
        self.assertNotIn('2+3', str(result))

    def test_detects_seeded_defects_and_accepts_corrected_observations(self):
        # Trusted test fixtures, not generated code or a sandbox acceptance run.
        fixtures = [
            ('calculator', '2+3', '5', lambda: str(2 - 3), lambda: str(2 + 3)),
            ('transform', 'ab', 'AB', lambda: 'ab'.lower(), lambda: 'ab'.upper()),
            ('tracker', 'save/read', 'retained', lambda: '', lambda: 'retained'),
        ]
        for name, request, expected, broken, fixed in fixtures:
            examples = [{'name': name, 'input': request, 'expected': expected}]
            evidence = {**self.evidence,
                        'examples_sha256': project_verification.examples_digest(examples)}
            for execute, status in ((broken, 'failed'), (fixed, 'tested')):
                evidence['results'] = [{'name': name, 'status': 'completed',
                                        'observed': execute()}]
                result = project_verification.verify(self.source, examples, evidence)
                self.assertEqual(result['status'], status)


if __name__ == '__main__':
    unittest.main()
