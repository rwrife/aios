"""A project bundle is data, never authority to execute on the host."""
import unittest

from aios import project_bundle


class ProjectBundleTests(unittest.TestCase):
    def test_valid_multifile_project_has_stable_content_digest(self):
        source = {
            'runtime': 'node-web',
            'entry': 'server/main.js',
            'files': {'server/main.js': 'console.log("ready")', 'web/index.html': '<h1>Hi</h1>'},
        }
        first = project_bundle.validate(source)
        second = project_bundle.validate({**source, 'files': dict(reversed(list(source['files'].items())))})
        self.assertEqual(first['sha256'], second['sha256'])
        self.assertEqual(first['entry'], 'server/main.js')
        self.assertEqual(len(first['files']), 2)
        source['files']['server/main.js'] = 'changed'
        self.assertNotEqual(first['files']['server/main.js'], source['files']['server/main.js'])
        self.assertNotEqual(first['sha256'], project_bundle.validate(source)['sha256'])

    def test_digest_binds_entry_and_content(self):
        one = {'runtime': 'node-web', 'entry': 'a.js',
               'files': {'a.js': 'x', 'b.js': 'y'}}
        two = {**one, 'entry': 'b.js'}
        three = {**one, 'files': {'a.js': 'z', 'b.js': 'y'}}
        self.assertNotEqual(project_bundle.validate(one)['sha256'],
                            project_bundle.validate(two)['sha256'])
        self.assertNotEqual(project_bundle.validate(one)['sha256'],
                            project_bundle.validate(three)['sha256'])

    def test_rejects_file_directory_collisions(self):
        for files in ({'web': 'x', 'web/a.js': 'y'}, {'web/a.js': 'y', 'WEB': 'x'}):
            with self.subTest(files=files), self.assertRaises(ValueError):
                project_bundle.validate({'runtime': 'node-web', 'entry': next(iter(files)),
                                         'files': files})

    def test_total_budget_counts_utf8_bytes(self):
        content = 'é' * (project_bundle.MAX_FILE_BYTES // 2)
        limit = project_bundle.MAX_TOTAL_BYTES // project_bundle.MAX_FILE_BYTES
        files = {f'f{i}.js': content for i in range(limit)}
        source = {'runtime': 'node-web', 'entry': 'f0.js', 'files': files}
        self.assertEqual(project_bundle.validate(source)['total_bytes'], project_bundle.MAX_TOTAL_BYTES)
        with self.assertRaises(ValueError):
            project_bundle.validate({**source, 'files': {**files, 'more.js': 'x'}})
        with self.assertRaises(ValueError):
            project_bundle.validate({**source, 'files': {'f0.js': content + 'é'}})

    def test_rejects_untrusted_paths_and_ambiguous_names(self):
        for path in ('../escape.js', '/tmp/x.js', 'a/../x.js', 'a//x.js',
                     'a/./x.js', 'a\\x.js', '.hidden/x.js', 'a/😀.js',
                     'a/.git/config', 'a/x\x00.js', 'a.js\n'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                project_bundle.validate({'runtime': 'node-web', 'entry': path,
                                         'files': {path: 'ok'}})
        with self.assertRaises(ValueError):
            project_bundle.validate({'runtime': 'node-web', 'entry': 'web/A.js',
                                     'files': {'web/A.js': 'a', 'web/a.js': 'b'}})

    def test_rejects_unsupported_or_oversized_bundle(self):
        base = {'runtime': 'node-web', 'entry': 'server.js', 'files': {'server.js': 'ok'}}
        for change in ({'runtime': 'host-shell'}, {'entry': 'missing.js'},
                       {'extra': 'unexpected'}, {'files': {'server.js': b'bytes'}},
                       {'files': {'server.js': '\ud800'}},
                       {'files': {'server.js': 'x' * (project_bundle.MAX_FILE_BYTES + 1)}}):
            with self.subTest(change=str(change)[:80]), self.assertRaises(ValueError):
                project_bundle.validate({**base, **change})

    def test_malformed_mapping_keys_fail_with_value_error(self):
        for source in ({'runtime': 'node-web', 'entry': 'x.js', 'files': {'x.js': 'x'},
                        1: 'strange', 'z': 'strange'},
                       {'runtime': 'node-web', 'entry': 'x.js',
                        'files': {'x.js': 'x', 1: 'strange'}}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                project_bundle.validate(source)

    def test_rejects_invalid_shape_and_file_count(self):
        for source in (None, [], {'runtime': 'node-web'},
                       {'runtime': 'node-web', 'entry': 'x.js', 'files': []},
                       {'runtime': 'node-web', 'entry': 'x.js', 'files': {}},
                       {'runtime': 'node-web', 'entry': 'x.js', 'files':
                        {f'f{i}.js': 'x' for i in range(project_bundle.MAX_FILES + 1)}}):
            with self.subTest(source=str(source)[:60]), self.assertRaises(ValueError):
                project_bundle.validate(source)



if __name__ == '__main__':
    unittest.main()
