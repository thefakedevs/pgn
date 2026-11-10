"""Safety and rollback tests; Docker/network operations are replaced with fixtures."""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance', PROJECT / 'scripts/pgn_maintenance.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MaintenanceTests(unittest.TestCase):
    def test_xray_download_requires_matching_sha256(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as z:
            z.writestr('xray', b'\x7fELFfixture')
        data = output.getvalue()
        good = ('SHA2-256= ' + hashlib.sha256(data).hexdigest() + '\n').encode()
        with patch.object(m.platform, 'machine', return_value='x86_64'):
            with patch.object(m, 'asset', side_effect=[data, good]):
                self.assertEqual(m.xray_binary({}), b'\x7fELFfixture')
            with patch.object(m, 'asset', side_effect=[data, b'SHA256= ' + b'0' * 64]):
                with self.assertRaisesRegex(m.MaintenanceError, 'checksum'):
                    m.xray_binary({})

    def test_failed_candidate_does_not_change_running_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            override = root / 'config/node-runtime.compose.json'
            m.write_json(override, {'services': {'pg-node': {'image': 'old'}}})
            before = override.read_bytes()
            with patch.object(m, 'test_candidate', side_effect=m.MaintenanceError('config rejected')):
                compose = unittest.mock.Mock()
                with self.assertRaisesRegex(m.MaintenanceError, 'config rejected'):
                    m.apply_candidate(root, {}, compose, {}, 'candidate', {}, root)
                compose.assert_not_called()
            self.assertEqual(before, override.read_bytes())

    def test_failed_restart_restores_image_and_original_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            override = root / 'config/node-runtime.compose.json'
            m.write_json(override, {'services': {'pg-node': {'image': 'original'}}})
            before = override.read_bytes()
            backup = root / 'backup'
            m.write_json(backup / 'before.json', {'rollback_tag': 'local/pgn-node:rollback-fixture'})
            calls = []
            def compose(root, values, *args, **kwargs):
                calls.append((args, json.loads(override.read_text())))
            with patch.object(m, 'test_candidate'), patch.object(m, 'wait_for_node', side_effect=[m.MaintenanceError('failed'), None]):
                with self.assertRaisesRegex(m.MaintenanceError, 'failed'):
                    m.apply_candidate(root, {}, compose, {'Config': {'Image': 'original'}}, 'candidate', {}, backup)
            starts = [c for c in calls if c[0][0] == 'up']
            self.assertEqual(len(starts), 2)
            self.assertEqual(starts[1][1]['services']['pg-node']['image'], 'local/pgn-node:rollback-fixture')
            self.assertTrue(all(c[0][-1] == 'pg-node' and '--no-deps' in c[0] for c in starts))
            self.assertEqual(before, override.read_bytes())

    def test_failed_rollback_keeps_explicit_recovery_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / 'backup'
            m.write_json(backup / 'before.json', {'rollback_tag': 'local/pgn-node:rollback-fixture'})
            with patch.object(m, 'test_candidate'), patch.object(m, 'wait_for_node', side_effect=m.MaintenanceError('failed')):
                with self.assertRaisesRegex(m.MaintenanceError, 'rollback both failed'):
                    m.apply_candidate(root, {}, unittest.mock.Mock(), {'Config': {'Image': 'old'}}, 'candidate', {}, backup)
            result = json.loads((root / 'config/node-runtime.compose.json').read_text())
            self.assertEqual(result['services']['pg-node']['image'], 'local/pgn-node:rollback-fixture')

    def test_unknown_image_is_not_replaced_by_a_guessed_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            container = {'State': {'Running': True}, 'Config': {'Image': 'unknown/custom:tag'}}
            with patch.object(m, 'command', return_value=json.dumps([container])) as run:
                with self.assertRaisesRegex(m.MaintenanceError, 'refusing to guess'):
                    m.snapshot(root, {}, unittest.mock.Mock(return_value='fixture'), root)
                self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
