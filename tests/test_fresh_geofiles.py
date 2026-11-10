"""First-boot defaults, checksum failures and preservation of existing nodes."""
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance', PROJECT / 'scripts/pgn_maintenance.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FreshGeofilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pending = self.root / 'state/node/.fresh-install.json'
        self.pending.parent.mkdir(parents=True)
        self.pending.write_text('{}')
        self.data = {'geoip.dat': b'geoip-fixture' * 200, 'geosite.dat': b'geosite-fixture' * 200}
        self.compose = Mock(side_effect=lambda root, values, *args, **kw:
                            '' if args[0] == 'ps' else json.dumps({'services': {'pg-node': {'image': 'pasarguard/node:v0.5.7'}}}))

    def asset(self, info, name):
        if name.endswith('.sha256sum'):
            target = name[:-10]
            return (hashlib.sha256(self.data[target]).hexdigest() + '  ' + target).encode()
        return self.data[name]

    def test_latest_russian_release_is_verified_before_first_start_and_loaded_once(self):
        override = self.root / 'config/node-runtime.compose.json'
        def command(args):
            self.assertFalse(override.exists())
            if args[:2] == ['docker', 'build']:
                source = Path(args[-1])
                for name, value in self.data.items():
                    self.assertEqual((source / name).read_bytes(), value)
            else:
                self.assertIn('--network', args)
                self.assertIn('none', args)
                self.assertIn('-test', args)
            return ''
        with patch.object(m, 'release', return_value={'tag_name': '202611101135'}) as release, \
                patch.object(m, 'asset', side_effect=self.asset), patch.object(m, 'command', side_effect=command), patch('builtins.print'):
            m.initial_geofiles(self.root, {}, self.compose)
            release.assert_called_once_with('runetfreedom/russia-v2ray-rules-dat')
            record = json.loads((self.root / 'config/node-runtime.json').read_text())
            self.assertEqual(record['geofiles_region'], 'russia')
            self.assertEqual(record['geofiles_release'], '202611101135')
            self.assertEqual(json.loads(override.read_text())['services']['pg-node']['image'], record['image'])
            m.initial_geofiles(self.root, {}, self.compose)
            release.assert_called_once()
        self.assertFalse(self.pending.exists())
        self.assertEqual(list((self.root / 'state/bootstrap').iterdir()), [])

    def test_bad_checksum_blocks_first_boot_and_can_be_retried(self):
        with patch.object(m, 'release', return_value={'tag_name': 'fixture'}), \
                patch.object(m, 'asset', side_effect=[self.data['geoip.dat'], b'0' * 64]), patch.object(m, 'command') as command:
            with self.assertRaisesRegex(m.MaintenanceError, 'checksum mismatch'):
                m.initial_geofiles(self.root, {}, self.compose)
            command.assert_not_called()
        self.assertTrue(self.pending.exists())
        self.assertFalse((self.root / 'config/node-runtime.compose.json').exists())

    def test_xray_rejecting_databases_does_not_commit_the_image(self):
        with patch.object(m, 'release', return_value={'tag_name': 'fixture'}), \
                patch.object(m, 'asset', side_effect=self.asset), \
                patch.object(m, 'command', side_effect=['', m.MaintenanceError('Xray rejected files')]):
            with self.assertRaisesRegex(m.MaintenanceError, 'Xray rejected'):
                m.initial_geofiles(self.root, {}, self.compose)
        self.assertTrue(self.pending.exists())
        self.assertFalse((self.root / 'config/node-runtime.compose.json').exists())

    def test_existing_node_or_config_or_override_is_preserved_without_network(self):
        for kind in ['no-marker', 'container', 'generated', 'override']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                pending = root / 'state/node/.fresh-install.json'
                pending.parent.mkdir(parents=True)
                if kind != 'no-marker':
                    pending.write_text('{}')
                preserved = None
                if kind in ['generated', 'override']:
                    preserved = root / ('state/node/generated/xray.json' if kind == 'generated' else 'config/node-runtime.compose.json')
                    preserved.parent.mkdir(parents=True, exist_ok=True)
                    preserved.write_text('preserve-this')
                with patch.object(m, 'release') as release, patch.object(m, 'command') as command:
                    m.initial_geofiles(root, {}, Mock(return_value='existing-container'))
                    release.assert_not_called()
                    command.assert_not_called()
                if preserved:
                    self.assertEqual(preserved.read_text(), 'preserve-this')


if __name__ == '__main__':
    unittest.main()
