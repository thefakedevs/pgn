"""Safety and rollback tests; Docker/network operations are replaced with fixtures."""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import tarfile
import unittest
from unittest.mock import patch
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance', PROJECT / 'scripts/pgn_maintenance.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MaintenanceTests(unittest.TestCase):
    def test_first_up_prepares_missing_service_but_reuses_cached_image(self):
        for cached in [False, True]:
            with self.subTest(cached=cached), patch.object(m.subprocess, 'run') as run, patch.object(m, 'install') as install:
                run.return_value.returncode = 0 if cached else 1
                root, compose = Path('/pgn'), unittest.mock.Mock()
                m.ensure_service(root, {'MAINTENANCE_ENABLED': 'true'}, compose)
                self.assertEqual(install.call_count, 0 if cached else 1)
                run.assert_called_once()
        with patch.object(m.subprocess, 'run') as run, patch.object(m, 'install') as install:
            m.ensure_service(Path('/pgn'), {'MAINTENANCE_ENABLED': 'false'}, unittest.mock.Mock())
            run.assert_not_called()
            install.assert_not_called()

    def test_install_packages_the_unchanged_official_binary_without_starting_services(self):
        binary = b'\x7fELFofficial-fixture'
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as t:
            entry = tarfile.TarInfo('node-serviced')
            entry.size = len(binary)
            t.addfile(entry, io.BytesIO(binary))
        archive = buf.getvalue()
        name = 'node-serviced_0.0.4_Linux_x86_64.tar.gz'
        checksum = (hashlib.sha256(archive).hexdigest() + '  ' + name + '\n').encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(m.platform, 'machine', return_value='x86_64'), \
                    patch.object(m, 'release', return_value={}), \
                    patch.object(m, 'asset', side_effect=[archive, checksum]), patch('builtins.print'):
                compose = unittest.mock.Mock()
                m.install(root, {'MAINTENANCE_ENABLED': 'false'}, compose)
            self.assertEqual((root / 'state/maintenance/image/node-serviced').read_bytes(), binary)
            self.assertEqual(compose.call_args.args[-2:], ('build', 'maintenance'))
            self.assertEqual(compose.call_args.args[1]['MAINTENANCE_ENABLED'], 'true')
            self.assertFalse((root / '.env').exists())
            self.assertNotIn('golang', (root / 'state/maintenance/image/Dockerfile').read_text())
            with patch.object(m.platform, 'machine', return_value='x86_64'), \
                    patch.object(m, 'release', return_value={}), \
                    patch.object(m, 'asset', side_effect=[archive, b'0' * 64 + b'  ' + name.encode()]):
                with self.assertRaisesRegex(m.MaintenanceError, 'checksum mismatch'):
                    m.install(root, {}, unittest.mock.Mock())

    def test_each_update_changes_only_the_requested_component(self):
        for operation in ['update', 'core-update', 'geofiles']:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / '.env').write_text('API_KEY=keep-identity\n')
                (root / 'compose.yml').write_text('keep-existing-compose\n')
                old = {'xray': b'old-xray', 'geoip.dat': b'old-geoip', 'geosite.dat': b'old-geosite'}
                def snapshot(root, values, compose, backup):
                    for name, content in old.items():
                        (backup / name).write_bytes(content)
                    m.write_json(backup / 'before.json', {'rollback_tag': 'local/pgn-node:rollback-fixture'})
                    return {}, 'pasarguard/node:v0.5.7'
                def run(args):
                    return 'Xray 26.11.9 fixture' if 'version' in args else ''
                def apply(root, values, compose, container, candidate, record, backup):
                    expected_base = 'pasarguard/node:v0.5.8' if operation == 'update' else 'pasarguard/node:v0.5.7'
                    self.assertEqual(record['base_image'], expected_base)
                    self.assertEqual((backup / 'xray').read_bytes(), b'new-xray' if operation == 'core-update' else old['xray'])
                    for name in ['geoip.dat', 'geosite.dat']:
                        self.assertEqual((backup / name).read_bytes(), b'new-geo' * 200 if operation == 'geofiles' else old[name])
                args = argparse.Namespace(command=operation, version='latest', region='russia')
                with patch.object(m, 'snapshot', side_effect=snapshot), patch.object(m, 'command', side_effect=run), \
                        patch.object(m, 'release', return_value={'tag_name': 'v0.5.8'}), \
                        patch.object(m, 'xray_binary', return_value=b'new-xray'), \
                        patch.object(m, 'asset', return_value=b'new-geo' * 200), \
                        patch.object(m, 'apply_candidate', side_effect=apply) as applied, patch('builtins.print'):
                    m.update(root, args, {}, unittest.mock.Mock())
                    applied.assert_called_once()
                self.assertEqual((root / '.env').read_text(), 'API_KEY=keep-identity\n')
                self.assertEqual((root / 'compose.yml').read_text(), 'keep-existing-compose\n')

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
