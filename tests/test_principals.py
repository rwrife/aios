import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from aios import core
from aios.principals import current


class PrincipalTests(unittest.TestCase):
    def descriptor(self, **values):
        return json.dumps({'owner': str(uuid.uuid4()), 'uid': 20001,
                           'scope': str(uuid.uuid4()), 'workspace': '/workspace', **values})

    @patch('aios.principals.os.getuid', return_value=20001)
    def test_explicit_context_ignores_inherited_home(self, _):
        with patch.dict(os.environ, {'AIOS_PRINCIPAL': self.descriptor(),
                                     'HOME': '/home/other', 'XDG_CONFIG_HOME': '/private',
                                     'XDG_DATA_HOME': '/other'}):
            self.assertEqual(core.config_dir(), Path('/workspace/.aios/config'))
            self.assertEqual(core.data_dir(), Path('/workspace/.aios/data'))

    @patch('aios.principals.os.getuid', return_value=20001)
    def test_malformed_context_never_falls_back(self, _):
        for descriptor in ('{}', 'null', 'invalid', self.descriptor(workspace='/home/other'),
                           self.descriptor(uid=20002), self.descriptor(scope='other')):
            with self.subTest(descriptor=descriptor), patch.dict(os.environ, {'AIOS_PRINCIPAL': descriptor}):
                with self.assertRaises((ValueError, PermissionError)):
                    core.config_dir()

    @patch('aios.principals.os.getuid', return_value=20001)
    def test_anonymous_is_explicit_and_has_no_owner(self, _):
        with patch.dict(os.environ, {'AIOS_PRINCIPAL': self.descriptor(owner=None)}):
            self.assertIsNone(current().owner)
